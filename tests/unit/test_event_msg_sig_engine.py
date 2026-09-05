"""M4-2d2/2d3：消息关联与信号广播引擎语义测试（内存模式）。

覆盖：
- correlate_message -> IntermediateCatchEvent 停等触发（变量合并、token 续走）
- correlate_message -> 中断式 message 边界（宿主取消归档、token 走边界出边、
  订阅消费后无残留）；宿主正常完成后订阅随宿主撤销（再关联报无订阅）
- correlate_message -> 非中断式 signal 边界（宿主保留 spawn 并发线、订阅常驻
  可再次触发）
- correlate_message -> 事件子流程 message start：中断式接管实例 / 非中断
  spawn 且可多次触发
- 跨实例 1:1：未限定实例取注册序最早；无订阅 NotFoundException
- 实例内 message throw（IntermediateThrowEvent）：触发同实例 catch、token
  继续流转；无等待订阅静默丢弃
- message throw end：token 结束同时投递（触发另一并行分支 catch 收束实例）
- throw_signal 公共 API：跨实例广播全部命中订阅
"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import NotFoundException
from camunda.engine.process_engine import ProcessEngine
from camunda.parser.bpmn_parser import parse_bpmn_xml

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
"""


def wrap(body: str, decls: str = "") -> str:
    return HEAD + decls + body + "\n</bpmn:definitions>\n"


def msg_def(ref: str, name: str) -> str:
    return f'<bpmn:messageEventDefinition messageRef="{ref}"/>'


def sig_def(ref: str) -> str:
    return f'<bpmn:signalEventDefinition signalRef="{ref}"/>'


def _subs(engine: ProcessEngine):
    return engine._event_subs


# ---------------------------------------------------------------------------
# correlate_message -> 中间捕获事件
# ---------------------------------------------------------------------------
CATCH_FLOW = wrap(
    """
  <bpmn:process id="catch-flow" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:intermediateCatchEvent id="wait">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:intermediateCatchEvent>
    <bpmn:userTask id="ut"/>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="wait"/>
    <bpmn:sequenceFlow id="f2" sourceRef="wait" targetRef="ut"/>
    <bpmn:sequenceFlow id="f3" sourceRef="ut" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="orderReady"/>\n',
)


def test_correlate_message_fires_waiting_catch():
    engine = ProcessEngine()
    assert "catch-flow" in engine.deploy(parse_bpmn_xml(CATCH_FLOW))
    pi = engine.start_process_instance_by_key("catch-flow")
    # 停等中：注册了 message catch 订阅，无任务
    assert engine.create_task_query() == []
    assert len(_subs(engine)) == 1
    engine.correlate_message("orderReady", variables={"order": "X"})
    # 消息带变量合并进实例；token 续走到 userTask
    assert pi.variables["order"] == "X"
    (task,) = engine.create_task_query()
    assert task.task_definition_key == "ut"
    assert _subs(engine) == {}  # 触发即消费
    engine.complete_task(task.id)
    assert pi.is_completed


def test_correlate_message_unknown_name_raises():
    engine = ProcessEngine()
    assert "catch-flow" in engine.deploy(parse_bpmn_xml(CATCH_FLOW))
    engine.start_process_instance_by_key("catch-flow")
    with pytest.raises(NotFoundException, match="没有等待中的订阅"):
        engine.correlate_message("nobody-waits")


# ---------------------------------------------------------------------------
# correlate_message -> 边界事件（中断式 / 非中断式）
# ---------------------------------------------------------------------------
BOUNDARY_MSG_XML = wrap(
    """
  <bpmn:process id="bnd-msg" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="ut" name="审批"/>
    <bpmn:boundaryEvent id="b-cancel" attachedToRef="ut">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:boundaryEvent>
    <bpmn:endEvent id="main-end"/>
    <bpmn:endEvent id="b-end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="ut"/>
    <bpmn:sequenceFlow id="f2" sourceRef="ut" targetRef="main-end"/>
    <bpmn:sequenceFlow id="bf" sourceRef="b-cancel" targetRef="b-end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="cancel"/>\n',
)


def test_correlate_message_interrupting_boundary_cancels_host():
    engine = ProcessEngine()
    assert "bnd-msg" in engine.deploy(parse_bpmn_xml(BOUNDARY_MSG_XML))
    pi = engine.start_process_instance_by_key("bnd-msg")
    (task,) = engine.create_task_query()
    engine.correlate_message("cancel")
    # 宿主被中断：任务归档，token 走边界出边 -> 实例完成；订阅无残留
    assert pi.is_completed
    assert engine.create_task_query() == []
    assert _subs(engine) == {}
    # 实例结束后再关联 -> 无等待订阅
    with pytest.raises(NotFoundException):
        engine.correlate_message("cancel")


def test_correlate_message_boundary_sub_dropped_on_normal_complete():
    engine = ProcessEngine()
    assert "bnd-msg" in engine.deploy(parse_bpmn_xml(BOUNDARY_MSG_XML))
    pi = engine.start_process_instance_by_key("bnd-msg")
    (task,) = engine.create_task_query()
    engine.complete_task(task.id)  # 宿主正常离开 -> 边界订阅撤销
    assert pi.is_completed
    assert _subs(engine) == {}
    with pytest.raises(NotFoundException):
        engine.correlate_message("cancel")


BOUNDARY_SIG_XML = wrap(
    """
  <bpmn:process id="bnd-sig" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="ut" name="催办"/>
    <bpmn:boundaryEvent id="b-alarm" attachedToRef="ut" cancelActivity="false">
      <bpmn:signalEventDefinition signalRef="S1"/>
    </bpmn:boundaryEvent>
    <bpmn:endEvent id="main-end"/>
    <bpmn:endEvent id="b-end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="ut"/>
    <bpmn:sequenceFlow id="f2" sourceRef="ut" targetRef="main-end"/>
    <bpmn:sequenceFlow id="bf" sourceRef="b-alarm" targetRef="b-end"/>
  </bpmn:process>
""",
    '<bpmn:signal id="S1" name="alarm"/>\n',
)


def test_signal_noninterrupting_boundary_keeps_host_and_subscription():
    engine = ProcessEngine()
    assert "bnd-sig" in engine.deploy(parse_bpmn_xml(BOUNDARY_SIG_XML))
    pi = engine.start_process_instance_by_key("bnd-sig")
    (task,) = engine.create_task_query()
    hits = engine.throw_signal("alarm")
    assert hits == 1
    # 宿主保留（任务还在），并发线已走完边界出边并收束
    assert not pi.is_completed
    assert len(engine.create_task_query()) == 1
    # 订阅常驻：再次广播可再次触发
    assert engine.throw_signal("alarm") == 1
    engine.complete_task(task.id)  # 主线完成 -> 并发线已收束 -> 实例完成
    assert pi.is_completed
    assert _subs(engine) == {}  # 宿主离开 -> 订阅撤销


# ---------------------------------------------------------------------------
# correlate_message / throw_signal -> 事件子流程 message/signal start
# ---------------------------------------------------------------------------
ESC_MSG_XML = wrap(
    """
  <bpmn:process id="esc-msg" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="ut" name="主线"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="msg-start">
        <bpmn:messageEventDefinition messageRef="M1"/>
      </bpmn:startEvent>
      <bpmn:userTask id="esc-ut" name="补救"/>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="msg-start" targetRef="esc-ut"/>
      <bpmn:sequenceFlow id="ef2" sourceRef="esc-ut" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="ut"/>
    <bpmn:sequenceFlow id="f2" sourceRef="ut" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="escalate"/>\n',
)


def test_correlate_message_interrupting_esc_takes_over_instance():
    engine = ProcessEngine()
    assert "esc-msg" in engine.deploy(parse_bpmn_xml(ESC_MSG_XML))
    pi = engine.start_process_instance_by_key("esc-msg")
    (main_task,) = engine.create_task_query()
    assert main_task.task_definition_key == "ut"
    engine.correlate_message("escalate")
    # 中断式：主线任务归档，事件子流程接管 -> esc 任务出现
    assert engine.create_task_query() == [] or all(
        t.task_definition_key == "esc-ut" for t in engine.create_task_query()
    )
    tasks = engine.create_task_query()
    assert len(tasks) == 1 and tasks[0].task_definition_key == "esc-ut"
    assert not pi.is_completed
    engine.complete_task(tasks[0].id)
    assert pi.is_completed


ESC_SIG_XML = wrap(
    """
  <bpmn:process id="esc-sig" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="ut" name="主线"/>
    <bpmn:subProcess id="esc" triggeredByEvent="true">
      <bpmn:startEvent id="sig-start" isInterrupting="false">
        <bpmn:signalEventDefinition signalRef="S1"/>
      </bpmn:startEvent>
      <bpmn:serviceTask id="esc-run" name="记录" camunda:delegateExpression="${esc-run}"/>
      <bpmn:endEvent id="esc-end"/>
      <bpmn:sequenceFlow id="ef1" sourceRef="sig-start" targetRef="esc-run"/>
      <bpmn:sequenceFlow id="ef2" sourceRef="esc-run" targetRef="esc-end"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="ut"/>
    <bpmn:sequenceFlow id="f2" sourceRef="ut" targetRef="end"/>
  </bpmn:process>
""",
    '<bpmn:signal id="S1" name="tick"/>\n',
)


def test_signal_noninterrupting_esc_fires_repeatedly():
    engine = ProcessEngine()
    runs: list = []
    engine.register_delegate("esc-run", lambda v: runs.append(1))
    assert "esc-sig" in engine.deploy(parse_bpmn_xml(ESC_SIG_XML))
    pi = engine.start_process_instance_by_key("esc-sig")
    (main_task,) = engine.create_task_query()
    # 非中断式：广播两次 -> esc 跑两次，宿主线不受影响
    assert engine.throw_signal("tick") == 1
    assert engine.throw_signal("tick") == 1
    assert runs == [1, 1]
    assert len(engine.create_task_query()) == 1  # 主线任务仍在
    engine.complete_task(main_task.id)
    assert pi.is_completed


# ---------------------------------------------------------------------------
# 跨实例 1:1 与公共 throw_signal 广播
# ---------------------------------------------------------------------------
def test_correlate_message_cross_instance_picks_first_registered():
    engine = ProcessEngine()
    assert "catch-flow" in engine.deploy(parse_bpmn_xml(CATCH_FLOW))
    pi1 = engine.start_process_instance_by_key("catch-flow")
    pi2 = engine.start_process_instance_by_key("catch-flow")
    assert len(_subs(engine)) == 2
    # 1:1：一次关联只触发注册序最早的实例（correlate 后 token 停在 ut）
    engine.correlate_message("orderReady")
    (task1,) = engine.create_task_query(pi1.id)
    assert task1.task_definition_key == "ut"
    assert not engine.create_task_query(pi2.id)  # pi2 仍在停等
    engine.complete_task(task1.id)
    assert engine.get_process_instance(pi1.id).is_completed
    assert not engine.get_process_instance(pi2.id).is_completed
    assert len(_subs(engine)) == 1  # pi2 的 catch 订阅仍在
    # 第二次关联轮到 pi2
    engine.correlate_message("orderReady")
    (task2,) = engine.create_task_query(pi2.id)
    engine.complete_task(task2.id)
    assert engine.get_process_instance(pi2.id).is_completed
    assert _subs(engine) == {}


# ---------------------------------------------------------------------------
# 实例内 throw（IntermediateThrowEvent / EndEvent）
# THROW_ITE_XML：并行分支 A 等 catch(M1)，分支 B 抛 ITE message(M1) 后停 userTask
# ---------------------------------------------------------------------------
THROW_ITE_XML = wrap(
    """
  <bpmn:process id="throw-ite" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:parallelGateway id="fork"/>
    <bpmn:intermediateCatchEvent id="wait">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:intermediateCatchEvent>
    <bpmn:endEvent id="a-end"/>
    <bpmn:intermediateThrowEvent id="fire">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:intermediateThrowEvent>
    <bpmn:userTask id="ut"/>
    <bpmn:endEvent id="b-end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="fork"/>
    <bpmn:sequenceFlow id="f2" sourceRef="fork" targetRef="wait"/>
    <bpmn:sequenceFlow id="f3" sourceRef="wait" targetRef="a-end"/>
    <bpmn:sequenceFlow id="f4" sourceRef="fork" targetRef="fire"/>
    <bpmn:sequenceFlow id="f5" sourceRef="fire" targetRef="ut"/>
    <bpmn:sequenceFlow id="f6" sourceRef="ut" targetRef="b-end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M1" name="kick"/>\n',
)


def test_in_instance_message_throw_fires_sibling_catch():
    engine = ProcessEngine()
    assert "throw-ite" in engine.deploy(parse_bpmn_xml(THROW_ITE_XML))
    pi = engine.start_process_instance_by_key("throw-ite")
    # fork 后：A 停在 catch 等消息，B 抛完消息停在 userTask
    tasks = engine.create_task_query()
    assert len(tasks) == 1 and tasks[0].task_definition_key == "ut"
    # 抛出即触发同实例 catch：A 已走完（无 A 侧残留 token 等待）
    assert _subs(engine) == {}  # catch 订阅已消费
    engine.complete_task(tasks[0].id)
    assert pi.is_completed


def test_in_instance_message_throw_no_subscriber_silent():
    engine = ProcessEngine()
    xml = wrap(
        """
  <bpmn:process id="throw-none" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:intermediateThrowEvent id="fire">
      <bpmn:messageEventDefinition messageRef="M1"/>
    </bpmn:intermediateThrowEvent>
    <bpmn:userTask id="ut"/>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="fire"/>
    <bpmn:sequenceFlow id="f2" sourceRef="fire" targetRef="ut"/>
    <bpmn:sequenceFlow id="f3" sourceRef="ut" targetRef="end"/>
  </bpmn:process>
""",
        '<bpmn:message id="M1" name="lost"/>\n',
    )
    assert "throw-none" in engine.deploy(parse_bpmn_xml(xml))
    pi = engine.start_process_instance_by_key("throw-none")
    # 无订阅：消息丢弃，token 继续流转到 userTask
    (task,) = engine.create_task_query()
    assert task.task_definition_key == "ut"
    engine.complete_task(task.id)
    assert pi.is_completed


# message throw end：并行分支 B 完成任务到达 message end -> 触发 A 的 catch 收束实例
THROW_END_XML = wrap(
    """
  <bpmn:process id="throw-end" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:parallelGateway id="fork"/>
    <bpmn:intermediateCatchEvent id="wait">
      <bpmn:messageEventDefinition messageRef="M2"/>
    </bpmn:intermediateCatchEvent>
    <bpmn:endEvent id="a-end"/>
    <bpmn:userTask id="ut"/>
    <bpmn:endEvent id="b-end">
      <bpmn:messageEventDefinition messageRef="M2"/>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="fork"/>
    <bpmn:sequenceFlow id="f2" sourceRef="fork" targetRef="wait"/>
    <bpmn:sequenceFlow id="f3" sourceRef="wait" targetRef="a-end"/>
    <bpmn:sequenceFlow id="f4" sourceRef="fork" targetRef="ut"/>
    <bpmn:sequenceFlow id="f5" sourceRef="ut" targetRef="b-end"/>
  </bpmn:process>
""",
    '<bpmn:message id="M2" name="release"/>\n',
)


def test_message_end_throw_triggers_sibling_catch():
    engine = ProcessEngine()
    assert "throw-end" in engine.deploy(parse_bpmn_xml(THROW_END_XML))
    pi = engine.start_process_instance_by_key("throw-end")
    # A 停在 catch；B 停在 ut
    assert len(engine.create_task_query()) == 1
    # 完成 B -> 到达 message end -> 消息触发 A catch -> 两分支收束实例完成
    (task,) = engine.create_task_query()
    engine.complete_task(task.id)
    assert pi.is_completed
    assert _subs(engine) == {}


# ---------------------------------------------------------------------------
# throw_signal 公共 API：跨实例广播
# ---------------------------------------------------------------------------
def test_throw_signal_broadcasts_across_instances():
    engine = ProcessEngine()
    assert "bnd-sig" in engine.deploy(parse_bpmn_xml(BOUNDARY_SIG_XML))
    assert "catch-flow" in engine.deploy(parse_bpmn_xml(CATCH_FLOW))
    pi1 = engine.start_process_instance_by_key("bnd-sig")
    pi2 = engine.start_process_instance_by_key("bnd-sig")
    pi3 = engine.start_process_instance_by_key("catch-flow")
    assert engine.throw_signal("alarm", variables={"who": "ops"}) == 2
    # 两个 bnd-sig 实例各自 spawn 并发线（主线任务保留）；catch-flow 不受影响
    assert not engine.get_process_instance(pi1.id).is_completed
    assert not engine.get_process_instance(pi2.id).is_completed
    assert engine.create_task_query(pi1.id) and engine.create_task_query(pi2.id)
    assert not engine.get_process_instance(pi3.id).is_completed
    assert pi1.variables["who"] == "ops" and pi2.variables["who"] == "ops"
    # 无命中广播：静默返回 0
    assert engine.throw_signal("nobody") == 0
