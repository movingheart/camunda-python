"""M4-2b3：timer start 事件子流程（订阅/到期/中断/非中断）引擎测试。

覆盖：流程级 timer 看门狗中断实例、非中断 timer 并行 spawn（主线保留）、
embedded subProcess 内 timer 事件子流程（sub 级中断 + 收束复活）、sub 正常
收束撤销订阅（无残留 job/触发）、同步直通流程不注册订阅。持久化见 M4-2b5。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.engine.process_engine import ProcessEngine
from camunda.model.execution import ExecutionState, ProcessInstanceState
from camunda.parser.bpmn_parser import parse_bpmn_xml

BPMN_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
BPMN_TAIL = "</bpmn:definitions>\n"


class FakeClock:
    """可拨快的 fake 时钟（与 test_job/test_boundary 约定一致）。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime.now().replace(microsecond=0)

    def now(self) -> str:
        return self.t.strftime("%Y-%m-%dT%H:%M:%S")

    def advance(self, seconds: float) -> str:
        self.t += timedelta(seconds=seconds)
        return self.now()


@pytest.fixture(autouse=True)
def fake_clock() -> FakeClock:
    fc = FakeClock()
    clock.set_clock(fc.now)
    yield fc
    clock.reset_clock()


def e(tag: str, node_id: str, attrs: str = "", children: str = "") -> str:
    sp = f" {attrs}" if attrs else ""
    if children:
        return f'<bpmn:{tag} id="{node_id}"{sp}>{children}</bpmn:{tag}>'
    return f'<bpmn:{tag} id="{node_id}"{sp}/>'


def f(fid: str, src: str, tgt: str) -> str:
    return f'<bpmn:sequenceFlow id="{fid}" sourceRef="{src}" targetRef="{tgt}"/>'


def timer_evt(kind: str, text: str) -> str:
    k = {"duration": "timeDuration", "date": "timeDate", "cycle": "timeCycle"}[kind]
    return (
        f'<bpmn:timerEventDefinition><bpmn:{k} xsi:type="bpmn:tFormalExpression">'
        f"{text}</bpmn:{k}></bpmn:timerEventDefinition>"
    )


def subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, children=inner)


def event_subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, 'triggeredByEvent="true"', inner)


def timer_start(node_id: str, duration: str, interrupting: bool = True) -> str:
    attr = "" if interrupting else ' isInterrupting="false"'
    return e("startEvent", node_id, attr, timer_evt("duration", duration))


def deploy(engine: ProcessEngine, body: str, name: str) -> str:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M4-2b3" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))
    return name


def task_ids(engine: ProcessEngine, pi) -> set[str]:
    return {t.task_definition_key for t in engine.create_task_query(pi.id)}


def jobs(engine: ProcessEngine, pi) -> list:
    return eng_jobs(engine, pi.id)


def eng_jobs(engine: ProcessEngine, pi_id: str) -> list:
    return engine.create_job_query(process_instance_id=pi_id)


# ---------------------------------------------------------------------------
# 流程级：timer 看门狗（中断）
# ---------------------------------------------------------------------------
def test_timer_event_subprocess_interrupts_instance(fake_clock):
    """主线停等 userTask，timer 事件子流程到期 -> 中断实例主线、接管收尾。"""
    eng = ProcessEngine()
    esc_inner = (
        timer_start("ts", "PT5S")
        + e("userTask", "watchUt")
        + e("endEvent", "escEnd")
        + f("ef1", "ts", "watchUt")
        + f("ef2", "watchUt", "escEnd")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + e("userTask", "mainUt")
        + e("endEvent", "end")
        + f("f1", "start", "mainUt")
        + f("f2", "mainUt", "end")
        + event_subproc("esc", esc_inner),
        "t-esc-top",
    )
    pi = eng.start_process_instance_by_key("t-esc-top")
    assert task_ids(eng, pi) == {"mainUt"}
    (job,) = eng_jobs(eng, pi.id)
    assert job.job_type == "timer-event-start"
    assert job.execution_id == pi.root_execution.id  # 订阅挂流程实例 scope

    # 到期 -> 中断实例主线，事件子流程接管
    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1
    assert task_ids(eng, pi) == {"watchUt"}
    assert eng_jobs(eng, pi.id) == []  # 订阅消费（单发）
    assert {t.task_definition_key for t in pi.completed_tasks} == {"mainUt"}
    actives = [ex for ex in pi.executions.values() if ex.state == ExecutionState.ACTIVE]
    assert all(ex.activity_id in (None, "esc", "watchUt") for ex in actives)

    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


# ---------------------------------------------------------------------------
# 流程级：非中断 timer（并行 spawn，主线保留）
# ---------------------------------------------------------------------------
def test_timer_event_subprocess_noninterrupting_keeps_main(fake_clock):
    """isInterrupting=false：事件子流程与主线并行，互不干扰。"""
    eng = ProcessEngine()
    esc_inner = (
        timer_start("ts", "PT5S", interrupting=False)
        + e("userTask", "watchUt")
        + e("endEvent", "escEnd")
        + f("ef1", "ts", "watchUt")
        + f("ef2", "watchUt", "escEnd")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + e("userTask", "mainUt")
        + e("endEvent", "end")
        + f("f1", "start", "mainUt")
        + f("f2", "mainUt", "end")
        + event_subproc("esc", esc_inner),
        "t-esc-ni",
    )
    pi = eng.start_process_instance_by_key("t-esc-ni")
    assert task_ids(eng, pi) == {"mainUt"}

    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1
    # 主线任务保留 + 事件子流程任务并存（并行）
    assert task_ids(eng, pi) == {"mainUt", "watchUt"}
    assert eng_jobs(eng, pi.id) == []
    assert pi.state == ProcessInstanceState.ACTIVE

    # 事件子流程先收尾 -> 实例不完成（主线还在）；主线完成 -> 实例完成
    eng.complete_task(
        next(t for t in eng.create_task_query(pi.id) if t.task_definition_key == "watchUt").id
    )
    assert pi.state == ProcessInstanceState.ACTIVE
    assert task_ids(eng, pi) == {"mainUt"}
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


# ---------------------------------------------------------------------------
# embedded subProcess 内：timer 事件子流程（sub 级中断 + 收束复活）
# ---------------------------------------------------------------------------
def test_timer_event_subprocess_inside_embedded_sub(fake_clock):
    """sub 展开期订阅；到期中断 sub 内部主线，事件子流程收尾后 sub 复活出边。"""
    eng = ProcessEngine()
    esc_inner = (
        timer_start("ts", "PT4S")
        + e("userTask", "watchUt")
        + e("endEvent", "escEnd")
        + f("ef1", "ts", "watchUt")
        + f("ef2", "watchUt", "escEnd")
    )
    sub_inner = (
        e("startEvent", "is")
        + e("userTask", "innerUt")
        + e("endEvent", "ie")
        + f("if1", "is", "innerUt")
        + f("if2", "innerUt", "ie")
        + event_subproc("esc", esc_inner)
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", sub_inner)
        + e("userTask", "afterUt")
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "afterUt")
        + f("f3", "afterUt", "end"),
        "t-esc-sub",
    )
    pi = eng.start_process_instance_by_key("t-esc-sub")
    assert task_ids(eng, pi) == {"innerUt"}
    (job,) = eng_jobs(eng, pi.id)
    assert job.job_type == "timer-event-start"
    # 订阅挂在 sub scope（停驻 sub 的 SCOPE execution），不是 root
    sub_scope = next(ex for ex in pi.executions.values() if ex.activity_id == "sub")
    assert job.execution_id == sub_scope.id

    fake_clock.advance(4)
    assert eng.execute_due_jobs() == 1
    # sub 内部主线被中断（innerUt 归档），事件子流程停等 watchUt
    assert task_ids(eng, pi) == {"watchUt"}
    assert {t.task_definition_key for t in pi.completed_tasks} == {"innerUt"}
    assert eng_jobs(eng, pi.id) == []

    # 事件子流程收尾 -> sub 复活 -> 主线继续到 afterUt
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert task_ids(eng, pi) == {"afterUt"}
    assert pi.state == ProcessInstanceState.ACTIVE
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


# ---------------------------------------------------------------------------
# 订阅生命周期：正常收束撤销 / 同步直通不注册
# ---------------------------------------------------------------------------
def test_timer_event_subscription_removed_when_sub_ends(fake_clock):
    """sub 内部正常走完（未到 timer）-> 订阅撤销，之后到期无触发。"""
    eng = ProcessEngine()
    esc_inner = (
        timer_start("ts", "PT60S")
        + e("userTask", "watchUt")
        + e("endEvent", "escEnd")
        + f("ef1", "ts", "watchUt")
        + f("ef2", "watchUt", "escEnd")
    )
    sub_inner = (
        e("startEvent", "is")
        + e("userTask", "innerUt")
        + e("endEvent", "ie")
        + f("if1", "is", "innerUt")
        + f("if2", "innerUt", "ie")
        + event_subproc("esc", esc_inner)
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", sub_inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end"),
        "t-esc-cancel",
    )
    pi = eng.start_process_instance_by_key("t-esc-cancel")
    assert len(eng_jobs(eng, pi.id)) == 1  # 订阅存在

    eng.complete_task(eng.create_task_query(pi.id)[0].id)  # innerUt 完成 -> sub 收束
    assert pi.state == ProcessInstanceState.COMPLETED
    assert eng_jobs(eng, pi.id) == []  # 订阅已撤销
    fake_clock.advance(120)
    assert eng.execute_due_jobs() == 0  # 无残留触发


def test_timer_event_subscription_skipped_when_instance_completes_sync():
    """流程同步直通完成：scope 未停留 -> 不注册订阅（无孤儿 job）。"""
    eng = ProcessEngine()
    esc_inner = (
        timer_start("ts", "PT5S")
        + e("endEvent", "escEnd")
        + f("ef1", "ts", "escEnd")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + e("endEvent", "end")
        + f("f1", "start", "end")
        + event_subproc("esc", esc_inner),
        "t-esc-sync",
    )
    pi = eng.start_process_instance_by_key("t-esc-sync")
    assert pi.state == ProcessInstanceState.COMPLETED
    assert eng_jobs(eng, pi.id) == []
