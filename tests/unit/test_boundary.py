"""M4-1 边界事件 / asyncAfter 语义测试（内存模式）。

覆盖（时钟走可注入 fake clock，拨时间即到期）：
- 中断式 timer 边界：注册时机（userTask / asyncBefore 宿主）、到期触发取消宿主
  （任务归档 / actinst 结算 / token 走边界出边）、宿主正常离开撤销、date/duration
- 文档化差异守卫：非中断式 / timeCycle / 同步宿主 的运行时行为
- 过期 job 防御：宿主已离开后残留边界/async-after job 到期被丢弃
- asyncAfter：serviceTask 行为后拆分离开、XOR 到期重求值、asyncBefore 链式
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
    """可拨快的 fake 时钟（与 test_job.py 约定一致）。"""

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


def cond(expr: str) -> str:
    return f'<bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">{expr}</bpmn:conditionExpression>'


def deploy(engine: ProcessEngine, body: str, name: str) -> str:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M4-1" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))
    return name


def boundary(ev_id: str, host: str, kind: str, text: str, cancel: bool = True) -> str:
    """boundaryEvent 元素（timer 变体，平级于宿主，attachedToRef 归属）。"""
    attrs = f'attachedToRef="{host}"'
    if not cancel:
        attrs += ' cancelActivity="false"'
    return e("boundaryEvent", ev_id, attrs, timer_evt(kind, text))


def host_flow(host: str = "taskA", kind: str = "duration", text: str = "PT5S") -> str:
    """start -> host(userTask) -> endMain；边界 esc -> endEsc。"""
    return (
        e("startEvent", "start")
        + f("f0", "start", host)
        + e("userTask", host)
        + boundary("esc", host, kind, text)
        + f("f1", host, "endMain")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "endMain")
        + e("endEvent", "endEsc")
    )


# ---------------------------------------------------------------------------
# 中断式 timer 边界：注册 / 触发 / 取消
# ---------------------------------------------------------------------------
def test_boundary_duration_interrupts_user_task(fake_clock):
    """userTask 停等期间边界(duration)到期 -> 取消宿主、token 走边界出边完成。"""
    eng = ProcessEngine()
    deploy(eng, host_flow(), "b-interrupt")
    pi = eng.start_process_instance_by_key("b-interrupt")
    # 注册：任务停等 + timer-boundary job
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "taskA"
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-boundary" and job.node_id == "esc"
    assert job.duedate > fake_clock.now()
    # 未到期不触发
    assert eng.execute_due_jobs() == 0
    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1
    # 中断语义：任务取消归档、宿主 actinst 结算、token 走 esc 出边完成
    assert pi.is_completed
    assert eng.create_task_query() == []
    assert eng.create_job_query() == []
    hi = pi.activity_history
    host_ai = next(a for a in hi if a.activity_id == "taskA")
    assert host_ai.end_time is not None  # 宿主活动被中断 = 已结算
    assert any(a.activity_id == "esc" for a in hi)  # 边界事件留 actinst 痕迹
    assert any(a.activity_id == "endEsc" for a in hi)  # 走了中断路径
    archived = next(t for t in pi.completed_tasks if t.id == task.id)
    assert archived.end_time is not None


def test_complete_task_cancels_boundary_job(fake_clock):
    """宿主正常 complete -> 边界 job 撤销；此后轮询无动作（主路不受影响）。"""
    eng = ProcessEngine()
    deploy(eng, host_flow(), "b-cancel")
    pi = eng.start_process_instance_by_key("b-cancel")
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-boundary"
    (task,) = eng.create_task_query()
    eng.complete_task(task.id)
    assert pi.is_completed  # 主路 endMain
    assert eng.create_job_query() == []  # 边界 job 已删
    fake_clock.advance(60)
    assert eng.execute_due_jobs() == 0


def test_boundary_date_absolute_trigger(fake_clock):
    """timeDate 边界：duedate = 绝对时间点，到期触发。"""
    eng = ProcessEngine()
    deploy(eng, host_flow(kind="date", text="2026-09-02T10:00:10"), "b-date")
    pi = eng.start_process_instance_by_key("b-date")
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.duedate == "2026-09-02T10:00:10"
    fake_clock.advance(10)
    assert eng.execute_due_jobs() == 1
    assert pi.is_completed


# ---------------------------------------------------------------------------
# 文档化差异守卫：非中断式 / cycle / 同步宿主
# ---------------------------------------------------------------------------
def test_non_interrupting_boundary_on_subprocess_rejected():
    """subProcess 宿主 + cancelActivity=false 仍拒绝（M4-2b4 支持普通等待活动宿主）。

    非中断式并发线需脱离 sub 容器挂父 scope；root 兼任 sub 载体时容器推导存在
    歧义，暂缓 —— 文档化差异，见 ARCHITECTURE.md。
    """
    eng = ProcessEngine()
    sub_inner = (
        e("startEvent", "is")
        + e("userTask", "innerUt")
        + e("endEvent", "ie")
        + f("if1", "is", "innerUt")
        + f("if2", "innerUt", "ie")
    )
    body = (
        e("startEvent", "start")
        + e("subProcess", "sub", children=sub_inner)
        + boundary("esc", "sub", "duration", "PT5S", cancel=False)
        + f("f1", "start", "sub")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "end")
        + e("endEvent", "endEsc")
    )
    deploy(eng, body, "b-nonint-sub")
    with pytest.raises(InvalidRequestException, match="subProcess 宿主非中断式"):
        eng.start_process_instance_by_key("b-nonint-sub")


def test_boundary_timer_cycle_rejected():
    """边界 timerCycle 拒绝（与 timer-catch 一致：cycle 仅用于 timer start）。"""
    eng = ProcessEngine()
    deploy(eng, host_flow(kind="cycle", text="R3/PT5S"), "b-cycle")
    with pytest.raises(InvalidRequestException, match="timerCycle"):
        eng.start_process_instance_by_key("b-cycle")


def test_boundary_on_sync_service_task_never_registers():
    """同步 serviceTask 无等待窗口：边界不注册不触发（Camunda 语义一致）。"""
    eng = ProcessEngine()
    body = (
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e("serviceTask", "svc")
        + boundary("esc", "svc", "duration", "PT1S")
        + f("f1", "svc", "end")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "end")
        + e("endEvent", "endEsc")
    )
    deploy(eng, body, "b-sync")
    pi = eng.start_process_instance_by_key("b-sync")
    assert pi.is_completed and eng.create_job_query() == []


# ---------------------------------------------------------------------------
# asyncBefore 宿主 + 边界：失败重试窗口 / 成功离开 / userTask 停等
# ---------------------------------------------------------------------------
def test_async_before_host_failure_window_interrupted(fake_clock):
    """asyncBefore 行为失败降级重试期间，边界到期 -> 中断宿主、行为重试作废。"""
    eng = ProcessEngine()
    calls: list[int] = []

    def boom(v):
        calls.append(1)
        raise RuntimeError("boom")

    eng.register_delegate("boom", boom)
    body = (
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e("serviceTask", "svc", 'camunda:asyncBefore="true" camunda:delegateExpression="${boom}"')
        + boundary("esc", "svc", "duration", "PT3S")
        + f("f1", "svc", "endMain")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "endMain")
        + e("endEvent", "endEsc")
    )
    deploy(eng, body, "b-afail")
    pi = eng.start_process_instance_by_key("b-afail")
    assert sorted(j.job_type for j in eng.create_job_query(process_instance_id=pi.id)) == [
        "async-continuation",
        "timer-boundary",
    ]
    # 行为 job 执行 -> delegate 失败 -> retries 3->2 顺延 5s
    assert eng.execute_due_jobs() == 1
    assert len(calls) == 1
    (retry,) = [j for j in eng.create_job_query(process_instance_id=pi.id) if j.job_type == "async-continuation"]
    assert retry.retries == 2
    # 边界(3s)先于重试(5s)到期 -> 中断
    fake_clock.advance(3)
    assert eng.execute_due_jobs() == 1
    assert pi.is_completed and len(calls) == 1  # 重试被取消，不再调 delegate
    assert eng.create_job_query() == []


def test_async_before_host_success_drops_boundary(fake_clock):
    """asyncBefore 行为成功完成并离开宿主 -> 边界 job 撤销（不再可能触发）。"""
    eng = ProcessEngine()
    eng.register_delegate("ok", lambda v: {"done": True})
    body = (
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e("serviceTask", "svc", 'camunda:asyncBefore="true" camunda:delegateExpression="${ok}"')
        + boundary("esc", "svc", "duration", "PT3S")
        + f("f1", "svc", "end")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "end")
        + e("endEvent", "endEsc")
    )
    deploy(eng, body, "b-asucc")
    pi = eng.start_process_instance_by_key("b-asucc")
    assert eng.execute_due_jobs() == 1
    assert pi.is_completed and pi.variables.get("done") is True
    assert eng.create_job_query() == []
    fake_clock.advance(60)
    assert eng.execute_due_jobs() == 0


def test_async_before_user_task_boundary_kept_then_fires(fake_clock):
    """asyncBefore + userTask：行为（建任务）后仍停等宿主，边界继续有效可中断。"""
    eng = ProcessEngine()
    body = (
        e("startEvent", "start")
        + f("f0", "start", "taskB")
        + e("userTask", "taskB", 'camunda:asyncBefore="true"')
        + boundary("esc", "taskB", "duration", "PT5S")
        + f("f1", "taskB", "endMain")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "endMain")
        + e("endEvent", "endEsc")
    )
    deploy(eng, body, "b-aut")
    pi = eng.start_process_instance_by_key("b-aut")
    assert eng.execute_due_jobs() == 1  # async 行为：创建任务停等
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "taskB"
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-boundary"  # 行为后边界仍保留
    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1
    assert pi.is_completed and eng.create_task_query() == []


# ---------------------------------------------------------------------------
# 过期 job 防御：宿主已离开后残留作业到期被丢弃
# ---------------------------------------------------------------------------
def test_stale_boundary_job_discarded_when_host_left(fake_clock):
    """token 已离开宿主（模拟并发竞态残留）-> 到期边界 job 被丢弃而非误中断。"""
    from camunda.model.job import Job

    eng = ProcessEngine()
    deploy(eng, host_flow(), "b-stale")
    pi = eng.start_process_instance_by_key("b-stale")
    (task,) = eng.create_task_query()
    eng.complete_task(task.id)  # 正常离开（真实路径会 drop；此处人为保留模拟竞态）
    stale = Job(
        id="stale-boundary-1",
        job_type="timer-boundary",
        duedate=fake_clock.advance(0),
        created=fake_clock.now(),
        process_instance_id=pi.id,
        execution_id=pi.root_execution.id,
        node_id="esc",
    )
    eng._jobs[stale.id] = stale
    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 1  # 执行了但丢弃
    assert eng.create_job_query() == [] and pi.is_completed


# ---------------------------------------------------------------------------
# asyncAfter：行为后拆分离开 / XOR 到期重求值 / asyncBefore 链式
# ---------------------------------------------------------------------------
def test_async_after_service_task_splits_leave():
    """serviceTask asyncAfter：delegate 在命令内同步执行，离开推进拆成独立 job。"""
    eng = ProcessEngine()
    calls: list[int] = []
    eng.register_delegate("work", lambda v: (calls.append(1), {"worked": True})[1])
    body = (
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e("serviceTask", "svc", 'camunda:asyncAfter="true" camunda:delegateExpression="${work}"')
        + f("f1", "svc", "end")
        + e("endEvent", "end")
    )
    deploy(eng, body, "aa-svc")
    pi = eng.start_process_instance_by_key("aa-svc")
    assert len(calls) == 1 and not pi.is_completed  # 行为已执行、离开被拆分
    assert pi.root_execution.activity_id == "svc"
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "async-after" and job.node_id == "svc"
    assert eng.execute_due_jobs() == 1
    assert pi.is_completed and pi.variables.get("worked") is True
    assert len(calls) == 1  # delegate 不重跑


def test_async_after_exclusive_gateway_reevaluates(fake_clock):
    """XOR asyncAfter：选路推迟到 job 到期，条件按到期时刻变量重新求值。"""
    eng = ProcessEngine()
    body = (
        e("startEvent", "start")
        + f("f0", "start", "gw")
        + e("exclusiveGateway", "gw", 'camunda:asyncAfter="true"')
        + f'<bpmn:sequenceFlow id="fH" sourceRef="gw" targetRef="endH">{cond("${x == 1}")}</bpmn:sequenceFlow>'
        + f'<bpmn:sequenceFlow id="fL" sourceRef="gw" targetRef="endL">{cond("${x == 2}")}</bpmn:sequenceFlow>'
        + e("endEvent", "endH")
        + e("endEvent", "endL")
    )
    deploy(eng, body, "aa-xor")
    pi = eng.start_process_instance_by_key("aa-xor", {"x": 1})
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "async-after"
    pi.variables["x"] = 2  # 拆分后、job 执行前变量变化
    assert eng.execute_due_jobs() == 1
    assert pi.is_completed
    assert pi.activity_history[-1].activity_id == "endL"  # 按到期时 x==2 重求值


def test_async_before_after_chained():
    """asyncBefore + asyncAfter 链式：行为 job 完成后拆 async-after，两段作业推进。"""
    eng = ProcessEngine()
    calls: list[int] = []
    eng.register_delegate("chain", lambda v: (calls.append(1), None))
    body = (
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e(
            "serviceTask",
            "svc",
            'camunda:asyncBefore="true" camunda:asyncAfter="true" camunda:delegateExpression="${chain}"',
        )
        + f("f1", "svc", "end")
        + e("endEvent", "end")
    )
    deploy(eng, body, "aa-chain")
    pi = eng.start_process_instance_by_key("aa-chain")
    (job0,) = eng.create_job_query(process_instance_id=pi.id)
    assert job0.job_type == "async-continuation"  # 到达仅行为 job
    assert len(calls) == 0
    assert eng.execute_due_jobs() == 1
    assert len(calls) == 1 and not pi.is_completed
    (job1,) = eng.create_job_query(process_instance_id=pi.id)
    assert job1.job_type == "async-after"  # 行为完成 -> 拆离开 job
    assert eng.execute_due_jobs() == 1
    assert pi.is_completed and eng.create_job_query() == []


def test_stale_async_after_discarded(fake_clock):
    """async-after 到期时 token 已推进 -> 作业被丢弃（过期防御）。"""
    from camunda.model.job import Job

    eng = ProcessEngine()
    eng.register_delegate("work", lambda v: None)
    body = (
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e("serviceTask", "svc", 'camunda:asyncAfter="true" camunda:delegateExpression="${work}"')
        + f("f1", "svc", "end")
        + e("endEvent", "end")
    )
    deploy(eng, body, "aa-stale")
    pi = eng.start_process_instance_by_key("aa-stale")
    assert eng.execute_due_jobs() == 1  # 正常消费
    # 人为再放一条过期 async-after（模拟异常残留）：token 已到 end -> 丢弃
    stale = Job(
        id="stale-aa-1",
        job_type="async-after",
        duedate=fake_clock.now(),
        created=fake_clock.now(),
        process_instance_id=pi.id,
        execution_id=pi.root_execution.id,
        node_id="svc",
    )
    eng._jobs[stale.id] = stale
    assert eng.execute_due_jobs() == 1
    assert eng.create_job_query() == [] and pi.is_completed
