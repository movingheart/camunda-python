"""DMN 1.1/1.3 数据模型（M5，纯 dataclass，无解析/无 DB 依赖）。

设计要点（对齐 Camunda dmn-model 职责、保持与 BPMN 模型层同风格）：
- DmnModel 是「部署单元」：一份 *.dmn 文件可含多个 Decision
- M5 范围 = 决策表（decisionTable）；literalExpression / DRD（relation、
  invocation 等）解析期明确报错（文档化差异）
- 命名空间版本不校验（1.1 与 1.3 同构，按 localName 解析）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# DMN 决策表 hit policy（DMN 1.3 规范 §6.3.4）
HIT_POLICIES = {"UNIQUE", "FIRST", "ANY", "PRIORITY", "RULE ORDER", "COLLECT"}

# COLLECT 聚合子策略（hitPolicy="COLLECT" + aggregator 属性；缺省 = 全行列表）
AGGREGATORS = {"SUM", "MIN", "MAX", "COUNT"}


@dataclass
class DmnInput:
    """decisionTable 输入列：inputExpression 文本 = FEEL 表达式（变量/路径）。"""

    id: Optional[str] = None
    name: Optional[str] = None            # label 属性（列展示名，可缺）
    expression: str = ""                  # inputExpression 内文本（如 "amount"）
    type_ref: Optional[str] = None        # number / string / boolean（仅文档化）


@dataclass
class DmnOutput:
    """decisionTable 输出列：name = 结果变量键（缺省回退 label/id）。

    output_values：outputValues 声明的取值优先级序（仅 PRIORITY hitPolicy
    消费——下标越小优先级越高；其余策略忽略）。
    """

    id: Optional[str] = None
    name: Optional[str] = None
    label: Optional[str] = None
    type_ref: Optional[str] = None
    output_values: List[str] = field(default_factory=list)

    def result_key(self) -> str:
        """输出结果在 dict 里的键（Camunda 语义：name 优先，回退 label/id）。"""
        return self.name or self.label or self.id or "output"


@dataclass
class DmnRule:
    """决策表规则行：input/output entries 与列按下标一一对应。

    input_entries 元素为 None = 通配（空文本 / "-"，恒命中）；
    output_entries 元素为 None = 空输出（FEEL 空表达式，Camunda 用于
    COLLECT COUNT 语义）。
    """

    id: Optional[str] = None
    input_entries: List[Optional[str]] = field(default_factory=list)
    output_entries: List[Optional[str]] = field(default_factory=list)


@dataclass
class DecisionTable:
    """决策表：hitPolicy 决定多行命中的收敛方式。"""

    id: Optional[str] = None
    hit_policy: str = "UNIQUE"            # 缺省 = UNIQUE（DMN 规范默认）
    aggregator: Optional[str] = None      # 仅 COLLECT：SUM/MIN/MAX/COUNT
    inputs: List[DmnInput] = field(default_factory=list)
    outputs: List[DmnOutput] = field(default_factory=list)
    rules: List[DmnRule] = field(default_factory=list)


@dataclass
class Decision:
    """一个 decision（M5 仅承载 decisionTable 形态）。"""

    id: str
    name: Optional[str] = None
    decision_table: Optional[DecisionTable] = None


@dataclass
class DmnModel:
    """一份 *.dmn 部署单元（对齐 BpmnModel 的部署单元角色）。"""

    decisions: List[Decision] = field(default_factory=list)
    source_name: Optional[str] = None
    source_xml: Optional[str] = None

    def get_decision(self, key: str) -> Decision:
        for d in self.decisions:
            if d.id == key:
                return d
        raise KeyError(f"decision not found in model: {key!r}")

    def decision_keys(self) -> List[str]:
        return [d.id for d in self.decisions]
