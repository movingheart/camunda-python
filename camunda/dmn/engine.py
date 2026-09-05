"""DMN 决策引擎（M5-3）：决策表求值 + hitPolicy 收敛语义。

结果形态（与 Camunda DmnDecisionResult 的常用形态对齐、文档化差异见下）：
- UNIQUE / FIRST / ANY：单输出列 -> 标量；多输出列 -> dict{output键: 值}；
  无命中 -> None
- RULE ORDER / COLLECT（无聚合）：行结果列表（标量或 dict，按命中顺序）
- COLLECT + SUM/MIN/MAX：标量（要求恰好 1 个输出列）
- COLLECT + COUNT：命中行数（int）

文档化差异：
- UNIQUE 多行命中 -> 运行时 ExpressionEvaluationException（DMN 规范违例）
- ANY 各行输出不一致 -> 运行时报错
- PRIORITY 单输出列按 outputValues 优先级序取最高；多输出列不支持
- 无命中不抛异常返回 None（对齐 Camunda 空结果语义）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from camunda.common.exceptions import (
    ExpressionEvaluationException,
    NotFoundException,
)
from camunda.dmn.feel import eval_expression, eval_unary_test
from camunda.model.dmn import Decision, DecisionTable, DmnModel, DmnOutput, DmnRule


class DmnEngine:
    """独立 DMN 引擎（对齐 Camunda DecisionService 职责，可脱离 BPMN 单用）。"""

    def __init__(self) -> None:
        # key -> Decision（重复部署视为新版本，覆盖并版本 +1，对齐 ACT_RE_DECDEF）
        self._decisions: Dict[str, Decision] = {}
        self._decision_versions: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # RepositoryService 语义
    # ------------------------------------------------------------------
    def deploy(self, model: DmnModel) -> List[str]:
        """部署 DmnModel，返回 decision key 列表（重复 key 版本 +1）。"""
        keys: List[str] = []
        for dec in model.decisions:
            self._decisions[dec.id] = dec
            self._decision_versions[dec.id] = self._decision_versions.get(dec.id, 0) + 1
            keys.append(dec.id)
        return keys

    def get_decision(self, key: str) -> Decision:
        if key not in self._decisions:
            raise NotFoundException(f"未部署的决策定义: {key!r}")
        return self._decisions[key]

    def get_decision_version(self, key: str) -> int:
        return self._decision_versions.get(key, 0)

    def list_decisions(self) -> List[Dict[str, Any]]:
        """已部署决策列表（key / name / version，部署序）。"""
        return [
            {"key": d.id, "name": d.name, "version": self._decision_versions.get(d.id, 0)}
            for d in self._decisions.values()
        ]

    # ------------------------------------------------------------------
    # 决策求值
    # ------------------------------------------------------------------
    def evaluate_decision(self, key: str, variables: Optional[dict] = None) -> Any:
        """求值决策表，返回形态见模块 docstring。"""
        decision = self.get_decision(key)
        table = decision.decision_table
        if table is None:
            raise ExpressionEvaluationException(
                f"decision {key!r} 不含 decisionTable（M5 仅支持决策表）"
            )
        return self._evaluate_table(table, variables or {}, key)

    # ------------------------------------------------------------------
    # 决策表核心求值
    # ------------------------------------------------------------------
    def _evaluate_table(self, table: DecisionTable, variables: dict, key: str) -> Any:
        # 1) 输入列求值（inputExpression 文本对 variables）
        input_values = [
            eval_expression(inp.expression, variables) for inp in table.inputs
        ]
        # 2) 规则命中过滤（全列 unaryTests 与）
        hits: List[DmnRule] = []
        for rule in table.rules:
            matched = True
            for text, value in zip(rule.input_entries, input_values):
                if not eval_unary_test(text, value):
                    matched = False
                    break
            if matched:
                hits.append(rule)
        if not hits:
            return self._no_hit_result(table)
        # 3) hitPolicy 收敛
        policy = table.hit_policy
        if policy == "UNIQUE":
            if len(hits) > 1:
                raise ExpressionEvaluationException(
                    f"决策 {key!r} hitPolicy=UNIQUE 违例：{len(hits)} 条规则同时命中"
                )
            return self._row_result(table, hits[0], variables)
        if policy == "FIRST":
            return self._row_result(table, hits[0], variables)
        if policy == "ANY":
            results = [self._row_result(table, r, variables) for r in hits]
            first = results[0]
            if any(r != first for r in results[1:]):
                raise ExpressionEvaluationException(
                    f"决策 {key!r} hitPolicy=ANY 违例：命中规则输出不一致"
                )
            return first
        if policy == "PRIORITY":
            return self._priority_result(table, hits, variables, key)
        # RULE ORDER / COLLECT 共用行收集
        results = [self._row_result(table, r, variables) for r in hits]
        if policy == "RULE ORDER":
            return results
        return self._collect_result(table, results, hits)

    # ------------------------------------------------------------------
    # 收敛子策略
    # ------------------------------------------------------------------
    def _no_hit_result(self, table: DecisionTable) -> Any:
        agg = table.aggregator
        if table.hit_policy == "COLLECT" and agg == "COUNT":
            return 0
        if table.hit_policy == "COLLECT" and agg in ("SUM", "MIN", "MAX"):
            return None
        if table.hit_policy in ("RULE ORDER", "COLLECT"):
            return []
        return None  # UNIQUE / FIRST / ANY 无命中 -> 空结果（对齐 Camunda）

    def _row_result(
        self, table: DecisionTable, rule: DmnRule, variables: dict
    ) -> Any:
        """单行结果：单输出列 -> 标量；多输出列 -> dict。"""
        values = [
            eval_expression(text, variables) if text is not None else None
            for text in rule.output_entries
        ]
        if len(table.outputs) == 1:
            return values[0]
        return {out.result_key(): v for out, v in zip(table.outputs, values)}

    def _priority_result(
        self, table: DecisionTable, hits: List[DmnRule], variables: dict, key: str
    ) -> Any:
        if len(table.outputs) != 1:
            raise ExpressionEvaluationException(
                f"决策 {key!r} hitPolicy=PRIORITY 仅支持单输出列"
                f"（实际 {len(table.outputs)} 列）"
            )
        priority: List[str] = table.outputs[0].output_values
        if not priority:
            raise ExpressionEvaluationException(
                f"决策 {key!r} hitPolicy=PRIORITY 需要 output 声明 outputValues"
            )

        def rank(rule: DmnRule) -> int:
            text = rule.output_entries[0]
            val = eval_expression(text, variables) if text is not None else None
            for i, p in enumerate(priority):
                # 优先级按字面量文本比对（outputValues 里的原文）
                if _literal_text(p) == _literal_text(
                    text if text is not None else ""
                ):
                    return i
                if _values_loose_eq(val, p):
                    return i
            return len(priority)  # 未声明取值 = 最低优先级

        best = min(hits, key=rank)
        return self._row_result(table, best, variables)

    def _collect_result(
        self, table: DecisionTable, results: List[Any], hits: List[DmnRule]
    ) -> Any:
        agg = table.aggregator
        if agg is None:
            return results
        if agg == "COUNT":
            return len(hits)
        # SUM/MIN/MAX：要求单输出列数值
        if len(table.outputs) != 1:
            raise ExpressionEvaluationException(
                f"COLLECT {agg} 仅支持单输出列（实际 {len(table.outputs)} 列）"
            )
        nums = [v for v in results if v is not None]
        non_numeric = [v for v in nums if isinstance(v, bool) or not isinstance(v, (int, float))]
        if non_numeric:
            raise ExpressionEvaluationException(
                f"COLLECT {agg} 要求数值输出，遇到 {non_numeric[0]!r}"
            )
        if agg == "SUM":
            return sum(nums)
        if agg == "MIN":
            return min(nums) if nums else None
        return max(nums) if nums else None


def _literal_text(s: str) -> str:
    """去字符串字面量引号：'"A"' -> 'A'。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _values_loose_eq(val: Any, literal: str) -> bool:
    """求值结果与 outputValues 字面量的宽松比对。"""
    if isinstance(val, str):
        return val == _literal_text(literal)
    try:
        return val == float(literal)
    except (TypeError, ValueError):
        return False
