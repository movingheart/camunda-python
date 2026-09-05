"""M4-2d1：message/signal 事件模型与解析单元测试。

覆盖：
- signal 顶层声明收集 + signalEventDefinition signalRef 回填（start/catch/boundary）
- IntermediateThrowEvent / EndEvent 的 message/signal throw 解析
- 事件槽 4 元互斥（timer/error/message/signal）、未知 ref 部署报错
- timer throw 拒绝（文档化差异）
- 事件子流程 signal/message start（含非中断式）解析
- 边界 message/signal 变体归属宿主（中断/非中断 cancelActivity 语义保留）
运行时语义见引擎测试（M4-2d5）。
"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import DeploymentException
from camunda.model.bpmn import (
    BoundaryEvent,
    EndEvent,
    IntermediateCatchEvent,
    IntermediateThrowEvent,
    StartEvent,
    SubProcess,
)
from camunda.parser import parse_bpmn_xml

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
"""


def wrap(process_xml: str, decls_xml: str = "") -> str:
    """definitions 包装：可选在 process 前插顶层 signal/message 声明（M4-2d）。"""
    return HEAD + decls_xml + process_xml + "\n</bpmn:definitions>\n"


# ---------------------------------------------------------------------------
# signal 顶层声明 + catch / throw 事件定义
# ---------------------------------------------------------------------------
SIG_CATCH_XML = wrap(
    """
  <bpmn:process id="sig-p" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:intermediateCatchEvent id="sig-wait">
      <bpmn:signalEventDefinition signalRef="S1"/>
    </bpmn:intermediateCatchEvent>
    <bpmn:intermediateThrowEvent id="sig-fire">
      <bpmn:signalEventDefinition signalRef="S1"/>
    </bpmn:intermediateThrowEvent>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="sig-wait"/>
    <bpmn:sequenceFlow id="f2" sourceRef="sig-wait" targetRef="sig-fire"/>
    <bpmn:sequenceFlow id="f3" sourceRef="sig-fire" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:signal id="S1" name="alarm"/>\n',
)


def test_signal_declaration_and_events_parsed():
    model = parse_bpmn_xml(SIG_CATCH_XML)
    proc = model.get_process("sig-p")
    catch = proc.flow_nodes["sig-wait"]
    assert isinstance(catch, IntermediateCatchEvent)
    assert catch.signal_name == "alarm"
    assert catch.timer is None and catch.message_name is None
    fire = proc.flow_nodes["sig-fire"]
    assert isinstance(fire, IntermediateThrowEvent)
    assert fire.signal_name == "alarm"
    assert fire.message_name is None


# ---------------------------------------------------------------------------
# message throw：EndEvent / IntermediateThrowEvent
# ---------------------------------------------------------------------------
MSG_END_XML = wrap(
    """
  <bpmn:process id="msg-p" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:intermediateThrowEvent id="m-fire">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:intermediateThrowEvent>
    <bpmn:endEvent id="end">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="m-fire"/>
    <bpmn:sequenceFlow id="f2" sourceRef="m-fire" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="orderReady"/>\n',
)


def test_message_throw_parsed():
    model = parse_bpmn_xml(MSG_END_XML)
    proc = model.get_process("msg-p")
    ite = proc.flow_nodes["m-fire"]
    assert isinstance(ite, IntermediateThrowEvent)
    assert ite.message_name == "orderReady"
    end = proc.flow_nodes["end"]
    assert isinstance(end, EndEvent)
    assert end.message_name == "orderReady"
    assert end.error_code is None


# ---------------------------------------------------------------------------
# 互斥与非法组合拒绝
# ---------------------------------------------------------------------------
MSG_SIG_CONFLICT_XML = wrap(
    """
  <bpmn:process id="conflict" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:intermediateCatchEvent id="w">
      <bpmn:messageEventDefinition messageRef="M1"/>
      <bpmn:signalEventDefinition signalRef="S1"/>
    </bpmn:intermediateCatchEvent>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="w"/>
    <bpmn:sequenceFlow id="f2" sourceRef="w" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="m"/>\n<bpmn:signal id="S1" name="s"/>\n',
)


def test_message_signal_conflict_raises():
    with pytest.raises(DeploymentException, match="互斥"):
        parse_bpmn_xml(MSG_SIG_CONFLICT_XML)


UNKNOWN_SIG_REF_XML = wrap(
    """
  <bpmn:process id="bad-ref" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:intermediateCatchEvent id="w">
      <bpmn:signalEventDefinition signalRef="NOPE"/>
    </bpmn:intermediateCatchEvent>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="w"/>
    <bpmn:sequenceFlow id="f2" sourceRef="w" targetRef="end"/>
  </bpmn:process>
"""
)


def test_unknown_signal_ref_raises():
    with pytest.raises(DeploymentException, match="未知 signal 声明"):
        parse_bpmn_xml(UNKNOWN_SIG_REF_XML)


TIMER_THROW_XML = wrap(
    """
  <bpmn:process id="t-throw" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:intermediateThrowEvent id="tf">
      <bpmn:timerEventDefinition>
        <bpmn:timeDuration xsi:type="bpmn:tFormalExpression">PT5S</bpmn:timeDuration>
      </bpmn:timerEventDefinition>
    </bpmn:intermediateThrowEvent>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="tf"/>
    <bpmn:sequenceFlow id="f2" sourceRef="tf" targetRef="end"/>
  </bpmn:process>
"""
)


def test_timer_throw_raises():
    # 文档化差异：throw 事件只支持 message/signal，timer throw 部署即报错
    with pytest.raises(DeploymentException, match="不支持 timer throw"):
        parse_bpmn_xml(TIMER_THROW_XML)


# ---------------------------------------------------------------------------
# 事件子流程 signal/message start + 边界 message/signal 变体
# ---------------------------------------------------------------------------
SIG_ESC_XML = wrap(
    """
  <bpmn:process id="esc-sig" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="sig-start" isInterrupting="false">
        <bpmn:signalEventDefinition signalRef="S1"/>
      </bpmn:startEvent>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="sig-start" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:signal id="S1" name="alarm"/>\n',
)


def test_event_sub_signal_start_parsed():
    model = parse_bpmn_xml(SIG_ESC_XML)
    esc = model.get_process("esc-sig").flow_nodes["esc"]
    assert isinstance(esc, SubProcess) and esc.triggered_by_event is True
    start = esc.process.flow_nodes["sig-start"]
    assert isinstance(start, StartEvent)
    assert start.signal_name == "alarm"
    assert start.is_interrupting is False  # 非中断 signal start 合法


BOUNDARY_MSG_SIG_XML = wrap(
    """
  <bpmn:process id="bnd-ms" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="ut"/>
    <bpmn:boundaryEvent id="b-msg" attachedToRef="ut">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:boundaryEvent>
    <bpmn:boundaryEvent id="b-sig" attachedToRef="ut" cancelActivity="false">
      <bpmn:signalEventDefinition signalRef="S1"/>
    </bpmn:boundaryEvent>
    <bpmn:endEvent id="end"/>
    <bpmn:endEvent id="b-end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="ut"/>
    <bpmn:sequenceFlow id="f2" sourceRef="ut" targetRef="end"/>
    <bpmn:sequenceFlow id="bf1" sourceRef="b-msg" targetRef="b-end"/>
    <bpmn:sequenceFlow id="bf2" sourceRef="b-sig" targetRef="b-end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="cancel"/>\n<bpmn:signal id="S1" name="alarm"/>\n',
)


def test_boundary_message_signal_parsed():
    model = parse_bpmn_xml(BOUNDARY_MSG_SIG_XML)
    proc = model.get_process("bnd-ms")
    ut = proc.flow_nodes["ut"]
    assert set(ut.boundary_events) == {"b-msg", "b-sig"}
    b_msg = proc.flow_nodes["b-msg"]
    assert isinstance(b_msg, BoundaryEvent)
    assert b_msg.message_name == "cancel" and b_msg.cancel_activity is True
    b_sig = proc.flow_nodes["b-sig"]
    assert isinstance(b_sig, BoundaryEvent)
    assert b_sig.signal_name == "alarm" and b_sig.cancel_activity is False
