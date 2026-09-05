"""M4-1 边界事件 / asyncAfter 持久化与崩溃恢复测试。

验证点：
- timer-boundary 停等落库 -> 重启还原（job/任务/未结算 actinst）-> 到期中断宿主 ->
  库最终一致（RU 清空 / HI_TASKINST 归档带 end_time / HI_ACTINST 结算）
- async-after 停等落库 -> 重启 -> 到期离开完成
- store 模式下 asyncBefore 失败回滚 + 边界中断的组合一致性（重启后无残留运行态）
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.engine.process_engine import ProcessEngine
from camunda.parser.bpmn_parser import parse_bpmn_xml
from camunda.persistence.store import Store

BPMN_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
BPMN_TAIL = "</bpmn:definitions>\n"


class FakeClock:
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


def deploy(engine: ProcessEngine, body: str, name: str) -> None:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M4-1-P" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))


def hi_rows(db: str, table: str, pid: str) -> list:
    import sqlite3

    con = sqlite3.connect(db)
    rows = con.execute(f"SELECT * FROM {table} WHERE PROC_INST_ID_ = ?", (pid,)).fetchall()
    con.close()
    return rows


# start -> taskA(userTask) -> endMain；边界 esc(PT5S) -> endEsc
def host_flow(host: str = "taskA") -> str:
    return (
        e("startEvent", "start")
        + f("f0", "start", host)
        + e("userTask", host, 'name="审核"')
        + e(
            "boundaryEvent",
            "esc",
            f'attachedToRef="{host}"',
            timer_evt("duration", "PT5S"),
        )
        + f("f1", host, "endMain")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "endMain")
        + e("endEvent", "endEsc")
    )


def test_crash_recovery_timer_boundary(fake_clock, tmp_path):
    """边界停等落库 -> 重启还原（job/任务/actinst）-> 到期中断 -> 库一致收尾。"""
    from camunda.common.timers import format_iso, parse_iso

    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy(e1, host_flow(), "recover-b")
    pi1 = e1.start_process_instance_by_key("recover-b")
    (task,) = e1.create_task_query()
    (job,) = e1.create_job_query(process_instance_id=pi1.id)
    expect_due = format_iso(parse_iso(fake_clock.now()) + timedelta(seconds=5))
    assert job.job_type == "timer-boundary" and job.duedate == expect_due
    # 「崩溃」重启
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert pi2.id == pi1.id and not pi2.is_completed
    assert pi2.root_execution.activity_id == "taskA"
    (t2,) = e2.create_task_query()
    assert t2.id == task.id and t2.task_definition_key == "taskA"
    (j2,) = e2.create_job_query(process_instance_id=pi2.id)
    assert j2.id == job.id and j2.job_type == "timer-boundary"
    # 未结算 actinst 挂回 execution（供中断时结算）
    assert pi2.root_execution.open_activity is not None
    assert pi2.root_execution.open_activity.end_time is None
    # 到期触发 -> 中断完成
    fake_clock.advance(5)
    assert e2.execute_due_jobs() == 1
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_task_query() == [] and e2.create_job_query() == []
    # 库最终一致：RU 无活跃实例；HI_TASKINST 归档带 end_time；host actinst 结算
    assert Store(db).load_active_instances() == []
    (t_hi,) = hi_rows(db, "ACT_HI_TASKINST", pi1.id)
    assert t_hi[7] is not None  # END_TIME_ 非空（中断归档而非丢失）
    (a_hi,) = [r for r in hi_rows(db, "ACT_HI_ACTINST", pi1.id) if r[2] == "taskA"]
    assert a_hi[6] is not None  # taskA actinst 结算


def test_crash_recovery_async_after(fake_clock, tmp_path):
    """async-after 停等落库 -> 重启（delegate 重注册）-> 到期离开完成。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e(
            "serviceTask",
            "svc",
            'camunda:asyncAfter="true" camunda:delegateExpression="${work}"',
        )
        + f("f1", "svc", "end")
        + e("endEvent", "end"),
        "recover-aa",
    )
    e1.register_delegate("work", lambda v: {"done": True})
    pi1 = e1.start_process_instance_by_key("recover-aa")
    (job,) = e1.create_job_query(process_instance_id=pi1.id)
    assert job.job_type == "async-after"
    # 「崩溃」重启：bean 不落库需重注册（对齐 Camunda）
    e2 = ProcessEngine.from_database(db)
    e2.register_delegate("work", lambda v: {"done": True})
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed and pi2.root_execution.activity_id == "svc"
    (j2,) = e2.create_job_query(process_instance_id=pi2.id)
    assert j2.id == job.id and j2.job_type == "async-after"
    assert e2.execute_due_jobs() == 1
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.get_process_instance(pi2.id).variables.get("done") is True
    assert Store(db).load_active_instances() == []


def test_persisted_async_before_failure_window_boundary_interrupt(fake_clock, tmp_path):
    """store 模式：asyncBefore 失败降级（含回滚）后边界中断；重启无残留运行态。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    calls: list[int] = []

    def boom(v):
        calls.append(1)
        raise RuntimeError("boom")

    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "svc")
        + e(
            "serviceTask",
            "svc",
            'camunda:asyncBefore="true" camunda:delegateExpression="${boom}"',
        )
        + e(
            "boundaryEvent",
            "esc",
            'attachedToRef="svc"',
            timer_evt("duration", "PT3S"),
        )
        + f("f1", "svc", "endMain")
        + f("f2", "esc", "endEsc")
        + e("endEvent", "endMain")
        + e("endEvent", "endEsc"),
        "recover-afail",
    )
    e1.register_delegate("boom", boom)
    pi = e1.start_process_instance_by_key("recover-afail")
    assert e1.execute_due_jobs() == 1  # 行为失败：rollback + degrade（+5s 重试）
    assert len(calls) == 1
    types = sorted(j.job_type for j in e1.create_job_query(process_instance_id=pi.id))
    assert types == ["async-continuation", "timer-boundary"]  # 边界 job 回滚后仍在
    fake_clock.advance(3)  # 边界(3s)先于重试(5s)
    assert e1.execute_due_jobs() == 1
    assert e1.get_process_instance(pi.id).is_completed and len(calls) == 1
    assert e1.create_job_query() == []
    # 重启干净：实例已正常结束，RU 无残留
    assert ProcessEngine.from_database(db).list_process_instances() == []
