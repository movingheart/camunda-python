"""M4-2a1/M4-2b1：SubProcess 容器递归解析单元测试。

覆盖：内嵌子流程基本解析（内外容器隔离）、双层嵌套、边界事件挂子流程、
跨容器连线引用报错、子流程缺 startEvent 报错、事件子流程（triggeredByEvent）
解析校验（M4-2b1：error/message 事件定义回填、none start / 非中断 error /
未知 errorRef / 有入边 start 部署即报错）。运行时展开语义见引擎测试。
"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import DeploymentException
from camunda.model.bpmn import (
    BoundaryEvent,
    EndEvent,
    ServiceTask,
    StartEvent,
    SubProcess,
    UserTask,
)
from camunda.parser import parse_bpmn_xml

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
"""


def wrap(process_xml: str, decls_xml: str = "") -> str:
    """definitions 包装：可选在 process 前插顶层 error/message 声明（M4-2b）。"""
    return HEAD + decls_xml + process_xml + "\n</bpmn:definitions>\n"


EMBEDDED_XML = wrap("""
  <bpmn:process id="with-sub" name="含子流程" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="sub" name="内部处理">
      <bpmn:startEvent id="sub-start"/>
      <bpmn:serviceTask id="sub-task" camunda:delegateExpression="${audit}"/>
      <bpmn:endEvent id="sub-end"/>
      <bpmn:sequenceFlow id="sf1" sourceRef="sub-start" targetRef="sub-task"/>
      <bpmn:sequenceFlow id="sf2" sourceRef="sub-task" targetRef="sub-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="sub"/>
    <bpmn:sequenceFlow id="f2" sourceRef="sub" targetRef="end"/>
  </bpmn:process>
""")


def test_parse_embedded_subprocess():
    model = parse_bpmn_xml(EMBEDDED_XML)
    proc = model.get_process("with-sub")

    # 外层容器：subProcess 是普通节点，内部节点不泄漏到外层
    assert set(proc.flow_nodes) == {"start", "sub", "end"}
    sub = proc.flow_nodes["sub"]
    assert isinstance(sub, SubProcess)
    assert sub.name == "内部处理"
    assert sub.triggered_by_event is False
    assert sub.incoming == ["f1"]
    assert sub.outgoing == ["f2"]

    # 内部容器：独立 Process 实例，id 带 ::inner 后缀
    inner = sub.process
    assert inner.id == "sub::inner"
    assert set(inner.flow_nodes) == {"sub-start", "sub-task", "sub-end"}
    assert set(inner.sequence_flows) == {"sf1", "sf2"}
    # start_events 重算
    assert inner.start_events == [inner.flow_nodes["sub-start"]]
    # 内部节点解析为正确类型 + 扩展属性（serviceTask delegate）
    task = inner.flow_nodes["sub-task"]
    assert isinstance(task, ServiceTask)
    assert task.implementation_ref == "audit"
    assert task.incoming == ["sf1"]
    assert task.outgoing == ["sf2"]
    assert isinstance(inner.flow_nodes["sub-end"], EndEvent)


NESTED_XML = wrap("""
  <bpmn:process id="nested" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="outer">
      <bpmn:startEvent id="os"/>
      <bpmn:subProcess id="inner1">
        <bpmn:startEvent id="is"/>
        <bpmn:userTask id="it"/>
        <bpmn:endEvent id="ie"/>
        <bpmn:sequenceFlow id="if1" sourceRef="is" targetRef="it"/>
        <bpmn:sequenceFlow id="if2" sourceRef="it" targetRef="ie"/>
      </bpmn:subProcess>
      <bpmn:endEvent id="oe"/>
      <bpmn:sequenceFlow id="of1" sourceRef="os" targetRef="inner1"/>
      <bpmn:sequenceFlow id="of2" sourceRef="inner1" targetRef="oe"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="outer"/>
    <bpmn:sequenceFlow id="f2" sourceRef="outer" targetRef="end"/>
  </bpmn:process>
""")


def test_nested_subprocess_recursive_parse():
    model = parse_bpmn_xml(NESTED_XML)
    proc = model.get_process("nested")
    outer = proc.flow_nodes["outer"]
    assert isinstance(outer, SubProcess)
    outer_inner = outer.process
    # outer 容器内：start/end + 嵌套 subProcess，内部子孙不泄漏
    assert set(outer_inner.flow_nodes) == {"os", "inner1", "oe"}
    inner1 = outer_inner.flow_nodes["inner1"]
    assert isinstance(inner1, SubProcess)
    assert inner1.process.id == "inner1::inner"
    assert set(inner1.process.flow_nodes) == {"is", "it", "ie"}
    it = inner1.process.flow_nodes["it"]
    assert isinstance(it, UserTask)
    assert inner1.process.start_events == [inner1.process.flow_nodes["is"]]


BOUNDARY_SUB_XML = wrap("""
  <bpmn:process id="p-boundary-sub" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="sub">
      <bpmn:startEvent id="ss"/>
      <bpmn:userTask id="st"/>
      <bpmn:endEvent id="se"/>
      <bpmn:sequenceFlow id="sf1" sourceRef="ss" targetRef="st"/>
      <bpmn:sequenceFlow id="sf2" sourceRef="st" targetRef="se"/>
    </bpmn:subProcess>
    <bpmn:boundaryEvent id="b1" attachedToRef="sub" cancelActivity="true">
      <bpmn:timerEventDefinition>
        <bpmn:timeDuration xsi:type="bpmn:tFormalExpression">PT5M</bpmn:timeDuration>
      </bpmn:timerEventDefinition>
    </bpmn:boundaryEvent>
    <bpmn:endEvent id="ok"/>
    <bpmn:endEvent id="esc"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="sub"/>
    <bpmn:sequenceFlow id="f2" sourceRef="sub" targetRef="ok"/>
    <bpmn:sequenceFlow id="f3" sourceRef="b1" targetRef="esc"/>
  </bpmn:process>
""")


def test_boundary_event_attached_to_subprocess():
    model = parse_bpmn_xml(BOUNDARY_SUB_XML)
    proc = model.get_process("p-boundary-sub")
    sub = proc.flow_nodes["sub"]
    # 子流程是合法宿主：等待窗口 = 整段子流程执行期（M4-2a3 引擎语义）
    assert sub.boundary_events == ["b1"]
    b1 = proc.flow_nodes["b1"]
    assert isinstance(b1, BoundaryEvent)
    assert b1.attached_to == "sub"
    assert b1.timer is not None and b1.timer.kind == "duration"
    # 边界事件不出现在主流（无 incoming）
    assert b1.incoming == []


CROSS_CONTAINER_XML = wrap("""
  <bpmn:process id="cross" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="sub">
      <bpmn:startEvent id="ss"/>
      <bpmn:endEvent id="se"/>
      <bpmn:sequenceFlow id="sf1" sourceRef="ss" targetRef="end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="sub"/>
    <bpmn:sequenceFlow id="f2" sourceRef="sub" targetRef="end"/>
  </bpmn:process>
""")


def test_cross_container_reference_raises():
    # 内层连线指向外层节点 -> 在内层容器校验即报错（引用不存在于 sub::inner）
    with pytest.raises(DeploymentException, match="不存在于 process 'sub::inner'"):
        parse_bpmn_xml(CROSS_CONTAINER_XML)


NO_START_SUB_XML = wrap("""
  <bpmn:process id="no-start-sub" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="sub">
      <bpmn:userTask id="st"/>
      <bpmn:endEvent id="se"/>
      <bpmn:sequenceFlow id="sf1" sourceRef="st" targetRef="se"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="sub"/>
    <bpmn:sequenceFlow id="f2" sourceRef="sub" targetRef="end"/>
  </bpmn:process>
""")


def test_subprocess_without_start_raises():
    # 普通内嵌子流程必须至少一个内部 startEvent（缺 startEvent 校验递归进内层）
    with pytest.raises(DeploymentException, match="startEvent"):
        parse_bpmn_xml(NO_START_SUB_XML)


EVENT_SUB_XML = wrap(
    """
  <bpmn:process id="event-sub" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="err-start">
        <bpmn:errorEventDefinition errorRef="E1"/>
      </bpmn:startEvent>
      <bpmn:serviceTask id="cleanup"/>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="err-start" targetRef="cleanup"/>
      <bpmn:sequenceFlow id="ef2" sourceRef="cleanup" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:error id="E1" errorCode="ERR_1"/>\n',
)


def test_event_subprocess_strict_parse():
    """M4-2b1：事件子流程解析 —— errorRef 回填 errorCode、结构完整、无连线。"""
    model = parse_bpmn_xml(EVENT_SUB_XML)
    proc = model.get_process("event-sub")
    esc = proc.flow_nodes["esc"]
    assert isinstance(esc, SubProcess)
    assert esc.triggered_by_event is True
    # 事件子流程是父容器里的节点，但不参与 sequenceFlow（无入/出边）
    assert esc.incoming == [] and esc.outgoing == []
    inner = esc.process
    start = inner.flow_nodes["err-start"]
    assert isinstance(start, StartEvent)
    # errorRef=E1 -> 顶层 error errorCode="ERR_1" 解析期回填
    assert start.error_code == "ERR_1"
    assert start.timer is None and start.message_name is None
    # error start 默认 isInterrupting=true
    assert start.is_interrupting is True
    assert isinstance(inner.flow_nodes["cleanup"], ServiceTask)
    assert inner.start_events == [start]


EVENT_SUB_BAD_REF_XML = wrap(
    """
  <bpmn:process id="bad-ref" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="err-start">
        <bpmn:errorEventDefinition errorRef="NO_SUCH_ERROR"/>
      </bpmn:startEvent>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="err-start" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>
  </bpmn:process>
"""
)


def test_event_sub_unknown_error_ref_raises():
    # errorRef 指向未声明的 <error> -> 部署期报错（避免运行期查找空转）
    with pytest.raises(DeploymentException, match="未知 error 声明"):
        parse_bpmn_xml(EVENT_SUB_BAD_REF_XML)


EVENT_SUB_NONE_START_XML = wrap(
    """
  <bpmn:process id="none-start" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="plain-start"/>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="plain-start" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>
  </bpmn:process>
"""
)


def test_event_sub_none_start_raises():
    # 事件子流程内部 none start（无事件定义）无法被触发 -> 部署期报错
    with pytest.raises(DeploymentException, match="缺少事件定义"):
        parse_bpmn_xml(EVENT_SUB_NONE_START_XML)


EVENT_SUB_NONINTERRUPT_ERROR_XML = wrap(
    """
  <bpmn:process id="ni-error" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="err-start" isInterrupting="false">
        <bpmn:errorEventDefinition errorRef="E1"/>
      </bpmn:startEvent>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="err-start" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:error id="E1" errorCode="ERR_1"/>\n',
)


def test_event_sub_noninterrupting_error_start_raises():
    # BPMN 规范：错误事件只支持中断式 -> isInterrupting=false 部署期报错
    with pytest.raises(DeploymentException, match="错误事件只支持中断式"):
        parse_bpmn_xml(EVENT_SUB_NONINTERRUPT_ERROR_XML)


EVENT_SUB_MSG_XML = wrap(
    """
  <bpmn:process id="msg-sub" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="msc" triggeredByEvent="true">
      <bpmn:startEvent id="msg-start" isInterrupting="false">
        <bpmn:messageEventDefinition messageRef="M1"/>
      </bpmn:startEvent>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="msg-start" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="orderReady"/>\n',
)


def test_event_sub_message_start_parsed():
    """M4-2b1：message start 解析保留（消息投递入口后续里程碑，运行时报错）。"""
    model = parse_bpmn_xml(EVENT_SUB_MSG_XML)
    esc = model.get_process("msg-sub").flow_nodes["msc"]
    assert esc.triggered_by_event is True
    start = esc.process.flow_nodes["msg-start"]
    assert start.message_name == "orderReady"
    assert start.error_code is None and start.timer is None
    # 非中断 message start 解析允许（isInterrupting=false 对消息合法）
    assert start.is_interrupting is False


EVENT_SUB_INCOMING_XML = wrap(
    """
  <bpmn:process id="in-sub" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="err-start">
        <bpmn:errorEventDefinition errorRef="E1"/>
      </bpmn:startEvent>
      <bpmn:userTask id="ut"/>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="ut" targetRef="err-start"/>
      <bpmn:sequenceFlow id="ef2" sourceRef="err-start" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:error id="E1" errorCode="ERR_1"/>\n',
)


def test_event_sub_start_with_incoming_raises():
    # 事件子流程不参与 sequenceFlow：start 有入边 -> 部署期报错
    with pytest.raises(DeploymentException, match="有入边"):
        parse_bpmn_xml(EVENT_SUB_INCOMING_XML)


ERROR_END_XML = wrap(
    """
  <bpmn:process id="err-end" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="doit"/>
    <bpmn:endEvent id="boom">
      <bpmn:errorEventDefinition errorRef="E1"/>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="doit"/>
    <bpmn:sequenceFlow id="f2" sourceRef="doit" targetRef="boom"/>
  </bpmn:process>
""",
    '<bpmn:error id="E1" errorCode="ORDER_FAILED"/>\n',
)


def test_error_end_event_parsed():
    """M4-2b1：error 结束事件 —— endEvent 挂 errorCode（错误抛出口）。"""
    model = parse_bpmn_xml(ERROR_END_XML)
    boom = model.get_process("err-end").flow_nodes["boom"]
    assert isinstance(boom, EndEvent)
    assert boom.error_code == "ORDER_FAILED"
