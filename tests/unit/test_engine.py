"""流程引擎行为单元测试（M1 内存版）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from camunda.common.exceptions import NotFoundException, ProcessInstanceException
from camunda.engine import ProcessEngine
from camunda.parser import parse_bpmn_file

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# 纯文本内联简单流程（start -> service -> exclusive -> end）
INLINE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
  <bpmn:process id="inline" isExecutable="true">
    <bpmn:startEvent id="s"/>
    <bpmn:serviceTask id="svc" camunda:delegateExpression="${work}"/>
    <bpmn:exclusiveGateway id="gw" default="flow-low"/>
    <bpmn:userTask id="high" name="高优先级处理"/>
    <bpmn:endEvent id="done-high" name="走高端分支"/>
    <bpmn:endEvent id="done-low" name="走低端分支"/>
    <bpmn:sequenceFlow id="f1" sourceRef="s" targetRef="svc"/>
    <bpmn:sequenceFlow id="f2" sourceRef="svc" targetRef="gw"/>
    <bpmn:sequenceFlow id="flow-high" name="高" sourceRef="gw" targetRef="high">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">${priority == "high"}</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="flow-high-end" sourceRef="high" targetRef="done-high"/>
    <bpmn:sequenceFlow id="flow-low" name="低" sourceRef="gw" targetRef="done-low"/>
  </bpmn:process>
</bpmn:definitions>
"""


@pytest.fixture
def engine() -> ProcessEngine:
    return ProcessEngine()


def _inline_flow(engine: ProcessEngine, priority: str):
    """构造 inline 流程：svc 之后 gw 按 priority 分支。"""
    from camunda.parser import parse_bpmn_xml

    model = parse_bpmn_xml(INLINE_XML)
    calls = []

    def work(vars_):
        calls.append(vars_.get("x"))
        vars_["worked"] = True
        return None

    engine.register_delegate("work", work)
    engine.deploy(model)
    pi = engine.start_process_instance_by_key("inline", {"x": 1, "priority": priority})
    return pi, calls


def test_service_task_and_exclusive_gateway_low(engine):
    """条件为 false 时走 default 分支直接结束。"""
    pi, calls = _inline_flow(engine, "low")
    assert calls == [1]
    assert pi.variables.get("worked") is True
    assert pi.state.value == "COMPLETED"
    # 活动痕迹应含 svc 与 done-low，不含 high
    acts = [a.activity_id for a in pi.activity_history]
    assert "svc" in acts and "done-low" in acts and "high" not in acts


def test_exclusive_gateway_high_creates_user_task(engine):
    pi, _ = _inline_flow(engine, "high")
    assert pi.state.value == "ACTIVE", "应停在 userTask 等待"
    tasks = engine.create_task_query(process_instance_id=pi.id)
    assert len(tasks) == 1
    assert tasks[0].name == "高优先级处理"
    # 完成任务后走 done-high
    engine.complete_task(tasks[0].id, {"approved": True})
    assert pi.state.value == "COMPLETED"
    acts = [a.activity_id for a in pi.activity_history]
    assert "high" in acts and "done-high" in acts


def test_complete_already_done_task_raises(engine):
    pi, _ = _inline_flow(engine, "low")
    assert pi.state.value == "COMPLETED"
    # 该流程无任务产生，直接查任意任务 id 应 NotFound
    with pytest.raises(NotFoundException):
        engine.complete_task("no-such-task")


def test_undefined_variable_in_condition_raises(engine):
    """排他网关条件引用未定义变量 -> ProcessInstanceException。"""
    from camunda.parser import parse_bpmn_xml

    model = parse_bpmn_xml(INLINE_XML)
    engine.register_delegate("work", lambda v: None)
    engine.deploy(model)
    # 不传 priority -> flow-high 条件求值抛未定义
    with pytest.raises(ProcessInstanceException, match="priority"):
        engine.start_process_instance_by_key("inline", {"x": 1})


def test_unregistered_delegate_raises(engine):
    from camunda.parser import parse_bpmn_xml

    model = parse_bpmn_xml(INLINE_XML)
    engine.deploy(model)  # 未注册 work
    with pytest.raises(ProcessInstanceException, match="work"):
        engine.start_process_instance_by_key("inline", {"x": 1})


# ---------------------------------------------------------------------------
# 完整示例：贷款审批
# ---------------------------------------------------------------------------
def test_loan_approval_auto_pass(engine):
    model = parse_bpmn_file(str(EXAMPLES / "loan-approval.bpmn"))
    engine.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    engine.deploy(model)
    pi = engine.start_process_instance_by_key(
        "loan-approval", {"applicant": "张三", "amount": 5000}
    )
    assert pi.state.value == "COMPLETED"
    acts = [a.activity_id for a in pi.activity_history]
    assert "check-credit" in acts and "manual-review" not in acts
    assert acts[-1] == "end-approved"


def test_loan_approval_manual_review(engine):
    model = parse_bpmn_file(str(EXAMPLES / "loan-approval.bpmn"))
    engine.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    engine.deploy(model)
    pi = engine.start_process_instance_by_key(
        "loan-approval", {"applicant": "李四", "amount": 50000}
    )
    tasks = engine.create_task_query(process_instance_id=pi.id)
    assert len(tasks) == 1
    engine.complete_task(tasks[0].id, {"approved": False})
    assert pi.state.value == "COMPLETED"
    acts = [a.activity_id for a in pi.activity_history]
    assert acts[-1] == "end-rejected"


# ---------------------------------------------------------------------------
# 并行网关
# ---------------------------------------------------------------------------
def test_parallel_fork_join(engine):
    model = parse_bpmn_file(str(EXAMPLES / "parallel-review.bpmn"))
    engine.deploy(model)
    pi = engine.start_process_instance_by_key("parallel-review", {"subject": "x"})
    tasks = engine.create_task_query(process_instance_id=pi.id)
    assert len(tasks) == 2, "并行 fork 应产生 2 个待办"

    # 先完成第一个：仍应 ACTIVE（join 等第二个）
    engine.complete_task(tasks[0].id, {"a": 1})
    assert pi.state.value == "ACTIVE"
    remaining = engine.create_task_query(process_instance_id=pi.id)
    assert len(remaining) == 1

    engine.complete_task(remaining[0].id, {"b": 2})
    assert pi.state.value == "COMPLETED"
    # 汇聚后应恰好走完
    assert engine.create_task_query(process_instance_id=pi.id) == []


def test_start_unknown_key_raises(engine):
    with pytest.raises(NotFoundException, match="not-deployed"):
        engine.start_process_instance_by_key("not-deployed", {})
