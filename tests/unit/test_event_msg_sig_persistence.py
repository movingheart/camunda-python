"""M4-2d4：message/signal 订阅持久化崩溃恢复测试。

订阅本身不落库（纯内存派生态），恢复靠 _rebuild_event_subscriptions 从
execution 树重推导。验证点：
- catch 停等崩溃 -> 重启后 correlate_message 可触发（catch 订阅重推导）
- 事件子流程 message start（中断式）崩溃 -> 重启后关联接管实例
- 非中断 signal 边界停驻崩溃 -> 重启后 throw_signal 广播 spawn 并发线，
  主线保留、订阅常驻可再触发
- 恢复后无重复订阅（timer job 快照还原 + 订阅重放幂等，这里 msg/sig 无
  job，纯靠重放且不重复注册）
"""

from __future__ import annotations

import pytest

from camunda.engine.process_engine import ProcessEngine
from camunda.parser.bpmn_parser import parse_bpmn_xml
from camunda.persistence.store import Store

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
"""


def wrap(body: str, decls: str = "") -> str:
    return HEAD + decls + body + "\n</bpmn:definitions>\n"


def _subs(engine: ProcessEngine):
    return engine._event_subs


# start -> catch(msg) -> ut -> end
CATCH_FLOW = wrap(
    """
  <bpmn:process id="p-catch" isExecutable="true">
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

# start -> ut(主线) ；esc message start 中断式接管
ESC_MSG = wrap(
    """
  <bpmn:process id="p-esc-msg" isExecutable="true">
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

# start -> ut(主线) + 非中断 signal 边界
BOUNDARY_SIG = wrap(
    """
  <bpmn:process id="p-bnd-sig" isExecutable="true">
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


def test_recovery_catch_waiting_correlate_after_restart(tmp_path):
    """catch 停等崩溃 -> 重启重推导 catch 订阅 -> correlate 触发续走。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    assert "p-catch" in e1.deploy(parse_bpmn_xml(CATCH_FLOW))
    pi1 = e1.start_process_instance_by_key("p-catch")
    assert len(_subs(e1)) == 1
    # 「崩溃」重启：订阅不落库，靠 from_database 内部恢复重推导
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert pi2.id == pi1.id and not pi2.is_completed
    assert len(_subs(e2)) == 1  # catch 订阅重推导到位且无重复
    e2.correlate_message("orderReady", variables={"order": "R"})
    (task,) = e2.create_task_query()
    assert task.task_definition_key == "ut"
    assert pi2.variables["order"] == "R"
    assert _subs(e2) == {}  # 触发即消费
    e2.complete_task(task.id)
    assert e2.get_process_instance(pi1.id).is_completed


def test_recovery_esc_message_start_correlate_after_restart(tmp_path):
    """esc message start 停驻崩溃 -> 重启后关联接管（中断式）。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    assert "p-esc-msg" in e1.deploy(parse_bpmn_xml(ESC_MSG))
    pi1 = e1.start_process_instance_by_key("p-esc-msg")
    assert len(_subs(e1)) == 1  # 流程级 esc 订阅（root 兼任订阅容器）
    (main_task,) = e1.create_task_query()
    e2 = ProcessEngine.from_database(db)
    assert len(_subs(e2)) == 1  # 恢复重推导无重复
    (pi2,) = e2.list_process_instances()
    e2.correlate_message("escalate")
    # 中断式接管：主线任务归档（create_task_query 为空），esc 任务出现
    tasks = e2.create_task_query()
    assert len(tasks) == 1 and tasks[0].task_definition_key == "esc-ut"
    e2.complete_task(tasks[0].id)
    assert e2.get_process_instance(pi1.id).is_completed


def test_recovery_signal_boundary_broadcast_after_restart(tmp_path):
    """非中断 signal 边界停驻崩溃 -> 重启后广播 spawn 并发线、订阅常驻。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    assert "p-bnd-sig" in e1.deploy(parse_bpmn_xml(BOUNDARY_SIG))
    pi1 = e1.start_process_instance_by_key("p-bnd-sig")
    assert len(_subs(e1)) == 1
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    (main_task,) = e2.create_task_query()
    assert len(_subs(e2)) == 1  # 边界订阅重推导无重复
    # 广播 -> spawn 并发线（非中断：主线保留）；再广播可再触发
    assert e2.throw_signal("alarm") == 1
    assert e2.throw_signal("alarm") == 1
    assert len(_subs(e2)) == 1  # 常驻
    assert e2.create_task_query(pi1.id)  # 主线任务仍在
    assert not e2.get_process_instance(pi1.id).is_completed
    e2.complete_task(main_task.id)
    assert e2.get_process_instance(pi1.id).is_completed
    assert _subs(e2) == {}  # 宿主离开 -> 订阅撤销
