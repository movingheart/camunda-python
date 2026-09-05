"""DMN 1.1/1.3 XML 解析器（lxml，M5-1）。

职责（对齐 Camunda dmn-model 的解析部分、与 bpmn_parser 同风格）：
1. 解析 XML -> DmnModel（含多个 Decision）
2. decisionTable 结构：inputs（inputExpression 文本）/ outputs / rules
   （inputEntry/outputEntry 的 dmn:text 原文）
3. 校验：hitPolicy 合法、COLLECT aggregator 合法、entry 数量与列对齐、
   非 decisionTable 形态（literalExpression 等）明确报错

命名空间处理策略：只用 localName 分派（DMN 1.1/1.3 同构），不校验版本。
"""

from __future__ import annotations

from typing import List, Optional

from lxml import etree

from camunda.common.exceptions import DeploymentException
from camunda.model.dmn import (
    AGGREGATORS,
    HIT_POLICIES,
    Decision,
    DecisionTable,
    DmnInput,
    DmnModel,
    DmnOutput,
    DmnRule,
)


def _local(tag: str) -> str:
    """lxml tag 形如 {ns}localName -> localName。"""
    return tag.rsplit("}", 1)[-1]


def _children(el: etree._Element, local_name: str) -> List[etree._Element]:
    return [c for c in el if isinstance(c.tag, str) and _local(c.tag) == local_name]


def _text_of(el: Optional[etree._Element]) -> str:
    """取元素文本（None 安全）。"""
    return (el.text or "") if el is not None else ""


def _normalize_entry(text: str) -> Optional[str]:
    """单元格文本归一：空 / '-' -> None（通配或空输出）；其余 strip。"""
    t = text.strip()
    if t in ("", "-"):
        return None
    return t


def parse_dmn_xml(xml_text: str, source_name: Optional[str] = None) -> DmnModel:
    """解析 DMN XML 文本 -> DmnModel。失败抛 DeploymentException。"""
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise DeploymentException(f"DMN XML 语法错误: {e}") from e

    if _local(root.tag) != "definitions":
        raise DeploymentException(
            f"DMN 根元素须为 definitions，实际 {_local(root.tag)!r}"
        )

    model = DmnModel(source_name=source_name, source_xml=xml_text)
    for el in root:
        if not isinstance(el.tag, str):
            continue
        if _local(el.tag) == "decision":
            model.decisions.append(_parse_decision(el))
    if not model.decisions:
        raise DeploymentException("DMN definitions 未包含任何 decision")
    return model


def parse_dmn_file(path: str) -> DmnModel:
    """解析 .dmn 文件（demo/测试便捷入口）。"""
    with open(path, "r", encoding="utf-8") as f:
        return parse_dmn_xml(f.read(), source_name=path.rsplit("/", 1)[-1])


def _parse_decision(el: etree._Element) -> Decision:
    dec_id = el.get("id")
    if not dec_id:
        raise DeploymentException("decision 缺少 id 属性")
    dec = Decision(id=dec_id, name=el.get("name"))

    for child in el:
        if not isinstance(child.tag, str):
            continue
        ln = _local(child.tag)
        if ln == "decisionTable":
            dec.decision_table = _parse_decision_table(child)
        elif ln == "variable":
            continue  # decision 输出类型声明，M5 不消费
        else:
            # literalExpression / relation / invocation / context -> 明确报错
            raise DeploymentException(
                f"decision {dec_id!r} 仅支持 decisionTable 形态，"
                f"遇到不支持的子元素 {ln!r}（M5 文档化差异）"
            )
    if dec.decision_table is None:
        raise DeploymentException(f"decision {dec_id!r} 缺少 decisionTable")
    return dec


def _parse_decision_table(el: etree._Element) -> DecisionTable:
    hit_policy = (el.get("hitPolicy") or "UNIQUE").strip().upper()
    if hit_policy not in HIT_POLICIES:
        raise DeploymentException(
            f"未知 hitPolicy: {el.get('hitPolicy')!r}（支持 {sorted(HIT_POLICIES)}）"
        )
    aggregator = el.get("aggregation")
    aggregator = aggregator.strip().upper() if aggregator else None
    if aggregator is not None and aggregator not in AGGREGATORS:
        raise DeploymentException(
            f"未知 aggregation: {el.get('aggregation')!r}（支持 {sorted(AGGREGATORS)}）"
        )
    if aggregator is not None and hit_policy != "COLLECT":
        raise DeploymentException(
            f"aggregation 仅在 hitPolicy=COLLECT 下合法（当前 {hit_policy!r}）"
        )

    table = DecisionTable(
        id=el.get("id"), hit_policy=hit_policy, aggregator=aggregator
    )
    for child in el:
        if not isinstance(child.tag, str):
            continue
        ln = _local(child.tag)
        if ln == "input":
            table.inputs.append(_parse_input(child))
        elif ln == "output":
            table.outputs.append(_parse_output(child))
        elif ln == "rule":
            table.rules.append(_parse_rule(child))
        # annotation / informationRequirement 等忽略

    if not table.outputs:
        raise DeploymentException(
            f"decisionTable {table.id!r} 至少需要一个 output 列"
        )
    _validate_entry_alignment(table)
    return table


def _parse_input(el: etree._Element) -> DmnInput:
    expr_el = None
    for c in _children(el, "inputExpression"):
        expr_el = c
        break
    if expr_el is None:
        raise DeploymentException(f"input {el.get('id')!r} 缺少 inputExpression")
    return DmnInput(
        id=el.get("id"),
        name=el.get("label"),
        expression=_text_of(expr_el).strip(),
        type_ref=expr_el.get("typeRef"),
    )


def _parse_output(el: etree._Element) -> DmnOutput:
    return DmnOutput(
        id=el.get("id"),
        name=el.get("name"),
        label=el.get("label"),
        type_ref=el.get("typeRef"),
        output_values=_parse_output_values(el),
    )


def _parse_output_values(el: etree._Element) -> List[str]:
    """outputValues 子元素：逗号分隔的 FEEL 字面量列表（尊重引号内的逗号）。"""
    for child in el:
        if isinstance(child.tag, str) and _local(child.tag) == "outputValues":
            texts = _children(child, "text")
            raw = _text_of(texts[0]) if texts else ""
            parts: List[str] = []
            buf: List[str] = []
            in_quote = False
            for ch in raw:
                if ch == '"':
                    in_quote = not in_quote
                    buf.append(ch)
                elif ch == "," and not in_quote:
                    parts.append("".join(buf).strip())
                    buf = []
                else:
                    buf.append(ch)
            parts.append("".join(buf).strip())
            return [p for p in parts if p]
    return []


def _parse_rule(el: etree._Element) -> DmnRule:
    rule = DmnRule(id=el.get("id"))
    for child in el:
        if not isinstance(child.tag, str):
            continue
        ln = _local(child.tag)
        if ln == "inputEntry":
            rule.input_entries.append(_normalize_entry(_text_of(_children(child, "text")[0]) if _children(child, "text") else ""))
        elif ln == "outputEntry":
            texts = _children(child, "text")
            rule.output_entries.append(
                _normalize_entry(_text_of(texts[0]) if texts else "")
            )
        # description / annotationEntry 忽略
    return rule


def _validate_entry_alignment(table: DecisionTable) -> None:
    """规则行 entry 数量与列对齐校验（DMN 规范要求严格对应）。"""
    for rule in table.rules:
        if len(rule.input_entries) != len(table.inputs):
            raise DeploymentException(
                f"rule {rule.id!r} 的 inputEntry 数 {len(rule.input_entries)}"
                f" 与 input 列数 {len(table.inputs)} 不一致"
            )
        if len(rule.output_entries) != len(table.outputs):
            raise DeploymentException(
                f"rule {rule.id!r} 的 outputEntry 数 {len(rule.output_entries)}"
                f" 与 output 列数 {len(table.outputs)} 不一致"
            )
