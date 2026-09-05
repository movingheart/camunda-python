"""节点行为（M1 简化版）。

Camunda 用 PvmAtomicOperation 把「进入/离开节点」拆成可拦截的原子操作，
M1 以函数式 dispatch 呈现同样的语义，主推进循环在 process_engine 内。

本模块放**纯逻辑**（便于单测）：
- 排他网关选流规则（条件顺序求值 -> default -> 无条件兜底）
"""

from __future__ import annotations

from typing import List, Optional

from camunda.common.exceptions import ProcessInstanceException
from camunda.engine.expression import evaluate_condition
from camunda.model.bpmn import ExclusiveGateway, SequenceFlow


def select_exclusive_gateway_flow(
    gw: ExclusiveGateway,
    flows: List[SequenceFlow],
    variables: dict,
) -> SequenceFlow:
    """排他网关选流（Camunda 语义）：
    1. 按出边顺序取第一条条件为真的边（默认跳过 default）
    2. 无命中时走 default_flow
    3. 再兜底：无条件表达式（condition_expression is None）的出边
    4. 全不中 -> ProcessInstanceException（对应 Camunda 抛 NoOutgoingFlowsFound）
    """
    candidate = None
    for flow in flows:
        if flow.id == gw.default_flow:
            continue
        if flow.condition_expression is None:
            # 无条件边兜底候选，继续检查后面是否有真条件
            candidate = candidate or flow
            continue
        try:
            if evaluate_condition(flow.condition_expression, variables):
                return flow
        except ProcessInstanceException:
            raise  # 表达式错误直接上抛，便于定位
    if gw.default_flow:
        for flow in flows:
            if flow.id == gw.default_flow:
                return flow
    if candidate is not None:
        return candidate
    raise ProcessInstanceException(
        f"排他网关 {gw.id!r} 无任何出边条件满足，且无 default/无条件兜底边"
    )
