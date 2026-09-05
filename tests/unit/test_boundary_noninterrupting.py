"""M4-2b4：非中断式边界事件（cancelActivity=false）引擎测试。

覆盖（时钟走可注入 fake clock，拨时间即到期）：
- userTask 宿主 NI 触发：宿主不取消（任务/actinst 保留），并发线从边界出边走
- 完成时序 A/B：并发线先完/宿主先完（主线到 end 但并发线未收 -> 实例不提前
  完成，root 转 SCOPE 停驻等收束——M4-2b3 起事件子流程同样受益）
- embedded subProcess 内宿主 NI：sub 等并发线收束后才复活出边
- 宿主正常完成撤销：NI job 删除、无残留触发
- asyncBefore 宿主等待窗口内 NI 触发：行为续跑、并发线独立走边界出边
- 文档化差异守卫：subProcess 宿主 + cancelActivity=false 注册期明确报错
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.common.exceptions import InvalidRequestException
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


def boundary(ev_id: str, host: str, kind: str = "duration", text: str = "PT5S",
             cancel: bool = True) -> str:
    """boundaryEvent 元素（timer 变体，平级于宿主，attachedToRef 归属）。"""
    attrs = f'attachedToRef="{host}"'
    if not cancel:
        attrs += ' cancelActivity="false"'
    return e("boundaryEvent", ev_id, attrs, timer_evt(kind, text))


def subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, children=inner)


def deploy(engine: ProcessEngine, body: str, name: str) -> str:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M4-2b4" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))
    return name


def task_keys(engine: ProcessEngine, pi) -> set[str]:
    return {t.task_definition_key for t in engine.create_task_query(pi.id)}


def eng_jobs(engine: ProcessEngine, pi) -> list:
    return engine.create_job_query(process_instance_id=pi.id)


# ---------------------------------------------------------------------------
# userTask 宿主 NI：触发不取消宿主 + 完成时序 A（并发线先完）
# ---------------------------------------------------------------------------
def _ni_host_flow(watch_outside: bool = False) -> str:
    """start -> mainUt(宿主, NI esc->watchUt->endEsc) -> endMain。

    watch_outside=True 时并发线 watchUt/endEsc 定义为流程级节点（默认）。
    宿主停在 mainUt 等待；esc 到期 spawn 并发线停 watchUt，宿主保留。
    """
    return (
        e("startEvent", "start")
        + f("f0", "start", "mainUt")
        + e("userTask", "mainUt")
        + boundary("esc", "mainUt", cancel=False)
        + f("f1", "mainUt", "endMain")
        + f("f2", "esc", "watchUt")
        + f("f3", "watchUt", "endEsc")
        + e("userTask", "watchUt")
        + e("endEvent", "endMain")
        + e("endEvent", "endEsc")
    )


def test_ni_boundary_fires_without_canceling_host(fake_clock):
    """NI 到期：宿主任务保留、actinst 未结算，并发线 spawn 停 watchUt。"""
    eng = ProcessEngine()
    deploy(eng, _ni_host_flow(), "ni-basic")
    pi = eng.start_process_instance_by_key("ni-basic")
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "mainUt"
    (job,) = eng_jobs(eng, pi)
    assert job.job_type == "timer-boundary" and job.node_id == "esc"

    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1
    # 宿主保留 + 并发线停等并存
    assert task_keys(eng, pi) == {"mainUt", "watchUt"}
    assert eng_jobs(eng, pi) == []  # NI 单发：本 job 消费
    assert pi.state == ProcessInstanceState.ACTIVE
    # 宿主 actinst 未结算（未被取消）
    host_ai = next(a for a in pi.activity_history if a.activity_id == "mainUt")
    assert host_ai.end_time is None
    assert any(a.activity_id == "esc" for a in pi.activity_history)  # 边界痕迹


def test_ni_concurrent_line_finishes_first(fake_clock):
    """完成时序 A：并发线先收束 -> 实例 ACTIVE；宿主后完成 -> COMPLETED。"""
    eng = ProcessEngine()
    deploy(eng, _ni_host_flow(), "ni-order-a")
    pi = eng.start_process_instance_by_key("ni-order-a")
    fake_clock.advance(5)
    eng.execute_due_jobs()

    eng.complete_task(
        next(t for t in eng.create_task_query(pi.id) if t.task_definition_key == "watchUt").id
    )
    assert pi.state == ProcessInstanceState.ACTIVE
    assert task_keys(eng, pi) == {"mainUt"}  # 并发线收束，宿主仍在
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


def test_ni_host_finishes_before_concurrent_line(fake_clock):
    """完成时序 B：宿主先完成（主线到 end）-> 实例不提前完成；并发线收束后完成。

    回归守护 M4-2b3 起的事件子流程/并发线收束语义：root 到 end 不再等价
    实例完成——root 转 SCOPE 停驻等全部活跃子树收束。
    """
    eng = ProcessEngine()
    deploy(eng, _ni_host_flow(), "ni-order-b")
    pi = eng.start_process_instance_by_key("ni-order-b")
    fake_clock.advance(5)
    eng.execute_due_jobs()

    eng.complete_task(
        next(t for t in eng.create_task_query(pi.id) if t.task_definition_key == "mainUt").id
    )
    # 主线到 endMain，但并发线 watchUt 未收束 -> 实例必须保持 ACTIVE
    assert pi.state == ProcessInstanceState.ACTIVE
    assert task_keys(eng, pi) == {"watchUt"}
    # root 停驻等收束（主线已离开活动节点）
    assert pi.root_execution.activity_id is None
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


def test_ni_boundary_job_dropped_when_host_completes_first(fake_clock):
    """宿主在 NI 到期前正常完成 -> 边界 job 撤销，无残留触发、无并发线。"""
    eng = ProcessEngine()
    deploy(eng, _ni_host_flow(), "ni-cancel")
    pi = eng.start_process_instance_by_key("ni-cancel")
    (task,) = eng.create_task_query(pi.id)
    eng.complete_task(task.id)
    assert pi.state == ProcessInstanceState.COMPLETED
    assert eng_jobs(eng, pi) == []
    assert task_keys(eng, pi) == set()
    fake_clock.advance(60)
    assert eng.execute_due_jobs() == 0


# ---------------------------------------------------------------------------
# embedded subProcess 内宿主 NI：sub 复活等并发线收束
# ---------------------------------------------------------------------------
def test_ni_inside_embedded_sub_waits_concurrent_line(fake_clock):
    """sub 内 userTask 宿主 NI：宿主先完成 -> sub 不复活（并发线未收）；收束后复活。"""
    eng = ProcessEngine()
    sub_inner = (
        e("startEvent", "is")
        + f("f0", "is", "taskA")
        + e("userTask", "taskA")
        + boundary("esc", "taskA", cancel=False)
        + f("f1", "taskA", "ie")
        + f("f2", "esc", "watchUt")
        + f("f3", "watchUt", "endEsc")
        + e("userTask", "watchUt")
        + e("endEvent", "ie")
        + e("endEvent", "endEsc")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", sub_inner)
        + f("f0", "start", "sub")
        + f("f1", "sub", "afterUt")
        + e("userTask", "afterUt")
        + f("f2", "afterUt", "end")
        + e("endEvent", "end"),
        "ni-sub",
    )
    pi = eng.start_process_instance_by_key("ni-sub")
    fake_clock.advance(5)
    eng.execute_due_jobs()
    assert task_keys(eng, pi) == {"taskA", "watchUt"}

    # 宿主 taskA 完成 -> sub 内部主线收束，但并发线未收 -> sub 不得复活
    eng.complete_task(
        next(t for t in eng.create_task_query(pi.id) if t.task_definition_key == "taskA").id
    )
    assert pi.state == ProcessInstanceState.ACTIVE
    assert task_keys(eng, pi) == {"watchUt"}  # afterUt 未出现（sub 未复活）
    # 并发线收束 -> sub 复活沿出边到 afterUt
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert task_keys(eng, pi) == {"afterUt"}
    assert pi.state == ProcessInstanceState.ACTIVE
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


# ---------------------------------------------------------------------------
# asyncBefore 宿主等待窗口内 NI 触发
# ---------------------------------------------------------------------------
def test_ni_async_before_host_waiting_window(fake_clock):
    """asyncBefore 拆分后（行为未跑）NI 到期：宿主不取消，行为 job 续跑出主线。"""
    eng = ProcessEngine()
    calls: list[int] = []
    eng.register_delegate("work", lambda v: (calls.append(1), {"done": True})[1])
    body = (
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e(
            "serviceTask",
            "svc",
            'camunda:asyncBefore="true" camunda:delegateExpression="${work}"',
        )
        + boundary("esc", "svc", cancel=False)
        + f("f1", "svc", "end")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "end")
        + e("endEvent", "endEsc")
    )
    deploy(eng, body, "ni-asyncb")
    pi = eng.start_process_instance_by_key("ni-asyncb")
    # asyncBefore 拆分：async-continuation + timer-boundary 并存
    assert sorted(j.job_type for j in eng_jobs(eng, pi)) == [
        "async-continuation",
        "timer-boundary",
    ]
    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1  # NI 边界先到期（行为 job 未到期？都立即到期，按 duedate 排序）
    # 行为 job 消费后仍可能再 execute；断言宿主未取消：delegate 续跑或实例仍 ACTIVE
    eng.execute_due_jobs()
    assert len(calls) == 1  # async 行为执行（未被 NI 取消——中断式会作废行为）
    assert pi.state == ProcessInstanceState.COMPLETED  # 并发线 endEsc + 主线 end 收束


def test_ni_async_before_user_task_host(fake_clock):
    """asyncBefore + userTask 宿主：行为建任务后仍停等；NI 到期宿主保留并发线走。"""
    eng = ProcessEngine()
    body = (
        e("startEvent", "start")
        + f("f0", "start", "taskB")
        + e("userTask", "taskB", 'camunda:asyncBefore="true"')
        + boundary("esc", "taskB", cancel=False)
        + f("f1", "taskB", "endMain")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "endMain")
        + e("endEvent", "endEsc")
    )
    deploy(eng, body, "ni-aut")
    pi = eng.start_process_instance_by_key("ni-aut")
    assert eng.execute_due_jobs() == 1  # async 行为：建任务停等
    (task,) = eng.create_task_query(pi.id)
    assert task.task_definition_key == "taskB"

    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1  # NI 边界到期 -> 并发线 endEsc 收束
    assert pi.state == ProcessInstanceState.ACTIVE  # 宿主仍在
    assert task_keys(eng, pi) == {"taskB"}
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED
