"""M3 作业持久化 / 崩溃恢复测试：实例级与定义级 job 落库、重启还原、失败回滚。

与 test_job.py 共享时钟/构造 helper 约定（独立实现避免 import 测试模块耦合）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.engine.process_engine import ProcessEngine
from camunda.model.execution import ProcessInstanceState
from camunda.model.job import DEFAULT_MAX_RETRIES
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
        f"<bpmn:timerEventDefinition><bpmn:{k} xsi:type=\"bpmn:tFormalExpression\">"
        f"{text}</bpmn:{k}></bpmn:timerEventDefinition>"
    )


def deploy(engine: ProcessEngine, body: str, name: str) -> None:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M3-P" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))


def store_engine(db: str) -> ProcessEngine:
    return ProcessEngine(store=Store(db))


def test_crash_recovery_timer_catch(fake_clock, tmp_path):
    """timer-catch 停等实例落库 -> 重启 from_database -> 作业/执行还原 -> 拨钟触发完成。"""
    db = str(tmp_path / "camunda.db")
    e1 = store_engine(db)
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "t1")
        + e("userTask", "t1", 'name="do"')
        + f("f1", "t1", "wait")
        + e("intermediateCatchEvent", "wait", "", timer_evt("duration", "PT5S"))
        + f("f2", "wait", "end")
        + e("endEvent", "end"),
        "recover-catch",
    )
    pi = e1.start_process_instance_by_key("recover-catch")
    (task,) = e1.create_task_query()
    e1.complete_task(task.id)  # token -> wait，注册 timer-catch job 并落库
    (job,) = e1.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-catch"
    # 「崩溃」：全新引擎从库恢复
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert pi2.id == pi.id and not pi2.is_completed
    assert pi2.root_execution is not None
    assert pi2.root_execution.activity_id == "wait"
    assert len(e2.create_task_query()) == 0
    (job2,) = e2.create_job_query(process_instance_id=pi2.id)
    assert job2.id == job.id and job2.job_type == "timer-catch"
    # 到期触发 -> 流程在恢复后的引擎上完成
    fake_clock.advance(6)
    assert e2.execute_due_jobs() == 1
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_job_query() == []
    # 库最终一致：RU 清空、HI 收尾
    assert Store(db).load_active_instances() == []
    import sqlite3

    con = sqlite3.connect(db)
    (state, end_time) = con.execute(
        "SELECT STATE_, END_TIME_ FROM ACT_HI_PROCINST WHERE ID_ = ?", (pi.id,)
    ).fetchone()
    con.close()
    assert state == ProcessInstanceState.COMPLETED.value and end_time


def test_crash_recovery_timer_start(fake_clock, tmp_path):
    """定义级 timer-start 落库 -> 重启恢复 -> 触发启动实例 -> 库/引擎同步。"""
    db = str(tmp_path / "camunda.db")
    e1 = store_engine(db)
    deploy(
        e1,
        e("startEvent", "s0", "", timer_evt("duration", "PT10S"))
        + f("f0", "s0", "t1")
        + e("userTask", "t1", 'name="after"'),
        "recover-start",
    )
    (job,) = e1.create_job_query()
    assert job.is_definition_level and job.process_definition_key == "recover-start"
    assert [j.id for j in Store(db).load_timer_start_jobs()] == [job.id]
    # 重启恢复：定义级 job 挂回
    e2 = ProcessEngine.from_database(db)
    (job2,) = e2.create_job_query()
    assert job2.id == job.id and job2.duedate == job.duedate
    # 到点触发 -> 实例启动（停 userTask 已落库）-> 一次性 timer-start job 删除
    fake_clock.advance(11)
    assert e2.execute_due_jobs() == 1
    assert len(e2.list_process_instances()) == 1
    assert e2.create_job_query() == []
    assert Store(db).load_timer_start_jobs() == []
    # 再次重启：实例从 RU 还原，timer-start 不再出现
    e3 = ProcessEngine.from_database(db)
    (pi,) = e3.list_process_instances()
    assert len(e3.create_task_query(process_instance_id=pi.id)) == 1
    assert e3.create_job_query() == []


def test_crash_recovery_timer_start_cycle_keeps_repeat(fake_clock, tmp_path):
    """cycle timer-start 重启后 repeat 还原，触发后仍按剩余 count 续排。"""
    db = str(tmp_path / "camunda.db")
    e1 = store_engine(db)
    deploy(
        e1,
        e("startEvent", "s0", "", timer_evt("cycle", "R2/PT10S"))
        + f("f0", "s0", "end")
        + e("endEvent", "end"),
        "recover-cycle",
    )
    e2 = ProcessEngine.from_database(db)
    (job,) = e2.create_job_query()
    assert job.repeat == {"kind": "interval", "seconds": 10.0, "count": 2}
    # 第 1 次触发后重启：剩余 count=1 应持久化
    fake_clock.advance(11)
    assert e2.execute_due_jobs() == 1
    e3 = ProcessEngine.from_database(db)
    (job3,) = e3.create_job_query()
    assert job3.repeat["count"] == 1 and job3.duedate > fake_clock.now()
    # 第 2 次触发 -> count 耗尽 -> job 删除；直通实例完成即归档 HI
    fake_clock.advance(11)
    assert e3.execute_due_jobs() == 1
    assert e3.create_job_query() == []
    assert Store(db).load_timer_start_jobs() == []
    import sqlite3

    con = sqlite3.connect(db)
    (n,) = con.execute("SELECT COUNT(*) FROM ACT_HI_PROCINST").fetchone()
    con.close()
    assert n == 2  # 两次触发共 2 条历史实例


def test_persisted_async_failure_rolls_back(fake_clock, tmp_path):
    """持久化 asyncBefore 失败：内存回滚到上次同步点 + retries 递减，库与内存一致。"""
    db = str(tmp_path / "camunda.db")
    attempts: list[int] = []

    def flaky(vars_):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first-fails")

    e1 = store_engine(db)
    e1.register_delegate("flaky", flaky)
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "ft")
        + e(
            "serviceTask",
            "ft",
            'camunda:asyncBefore="true" camunda:delegateExpression="${flaky}"',
        )
        + f("f1", "ft", "end")
        + e("endEvent", "end"),
        "persist-retry",
    )
    e1.start_process_instance_by_key("persist-retry")
    # 首次执行失败 -> 回滚 + retries 3->2 + duedate 顺延（内存与库同步）
    e1.execute_due_jobs()
    assert len(attempts) == 1
    (job,) = e1.create_job_query()
    assert job.retries == DEFAULT_MAX_RETRIES - 1 and job.duedate > fake_clock.now()
    # 「崩溃」重启：库中 retries=2（rollback 没把内存半状态写进库）
    e2 = ProcessEngine.from_database(db)
    e2.register_delegate("flaky", flaky)
    (pi,) = e2.list_process_instances()
    assert not pi.is_completed and pi.root_execution.activity_id == "ft"
    (job2,) = e2.create_job_query()
    assert job2.id == job.id and job2.retries == 2
    # 重试成功 -> 流程完成
    fake_clock.advance(6)
    assert e2.execute_due_jobs() == 1
    assert len(attempts) == 2
    assert e2.get_process_instance(pi.id).is_completed
    assert e2.create_job_query() == []


def test_dead_letter_persisted_after_crash(fake_clock, tmp_path):
    """实例级 job 重试耗尽成死信后落库；重启仍可见死信（不再 acquire）。"""
    db = str(tmp_path / "camunda.db")

    def always_fail(vars_):
        raise RuntimeError("always")

    e1 = store_engine(db)
    e1.register_delegate("bad", always_fail)
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "bt")
        + e(
            "serviceTask",
            "bt",
            'camunda:asyncBefore="true" camunda:delegateExpression="${bad}"',
        )
        + f("f1", "bt", "end")
        + e("endEvent", "end"),
        "persist-dead",
    )
    e1.start_process_instance_by_key("persist-dead")
    for _ in range(DEFAULT_MAX_RETRIES):
        fake_clock.advance(6)
        e1.execute_due_jobs()
    (job,) = e1.create_job_query()
    assert job.is_dead()
    # 重启：死信 job 随实例还原，仍不被执行
    e2 = ProcessEngine.from_database(db)
    (job2,) = e2.create_job_query()
    assert job2.id == job.id and job2.retries == 0
    fake_clock.advance(60)
    assert e2.execute_due_jobs() == 0
    e2.delete_job(job2.id)  # 清理死信后库同步
    assert Store(db).load_active_instances()[0].jobs == []
