"""M3 作业语义测试（内存模式）：timer 停等/触发、asyncBefore 拆分、失败重试、死信。

时钟全部走可注入 clock（fixture fake_clock），拨时间即到期，无需真等待。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.common.exceptions import InvalidRequestException, NotFoundException
from camunda.engine.process_engine import ProcessEngine
from camunda.model.execution import ProcessInstanceState
from camunda.model.job import DEFAULT_MAX_RETRIES
from camunda.parser.bpmn_parser import parse_bpmn_xml

BPMN_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
BPMN_TAIL = "</bpmn:definitions>\n"


class FakeClock:
    """可拨快的 fake 时钟：now() 输出引擎定长 ISO（本地 naive）。"""

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


# ---------------------------------------------------------------------------
# BPMN 片段构造（节点自闭合；sequenceFlow 显式给）
# ---------------------------------------------------------------------------
def e(tag: str, node_id: str, attrs: str = "", children: str = "") -> str:
    sp = f" {attrs}" if attrs else ""
    if children:
        return f'<bpmn:{tag} id="{node_id}"{sp}>{children}</bpmn:{tag}>'
    return f'<bpmn:{tag} id="{node_id}"{sp}/>'


def f(fid: str, src: str, tgt: str) -> str:
    return f'<bpmn:sequenceFlow id="{fid}" sourceRef="{src}" targetRef="{tgt}"/>'


_KIND_TAG = {"duration": "timeDuration", "date": "timeDate", "cycle": "timeCycle"}


def timer_evt(kind: str, text: str) -> str:
    """timerEventDefinition 子元素（kind: duration | date | cycle）。"""
    k = _KIND_TAG[kind]
    return (
        f"<bpmn:timerEventDefinition><bpmn:{k} xsi:type=\"bpmn:tFormalExpression\">"
        f"{text}</bpmn:{k}></bpmn:timerEventDefinition>"
    )


def deploy(engine: ProcessEngine, body: str, name: str = "m3-test") -> str:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M3" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    keys = engine.deploy(parse_bpmn_xml(xml, source_name=name))
    assert name in keys
    return name


# 常用流程：start -> wait(timer) -> end
def catch_flow(pid: str, kind: str, text: str, wait_id: str = "wait") -> str:
    return (
        e("startEvent", "start")
        + f("f0", "start", wait_id)
        + e("intermediateCatchEvent", wait_id, "", timer_evt(kind, text))
        + f("f1", wait_id, "end")
        + e("endEvent", "end")
    )


def test_timer_duration_catch_waits_then_fires(fake_clock):
    """token 到 timer catch(duration) 停等注册 job；到期 execute_due_jobs 继续到 end。"""
    e1 = ProcessEngine()
    deploy(e1, catch_flow("dur-catch", "duration", "PT5S"), "dur-catch")
    pi = e1.start_process_instance_by_key("dur-catch")
    assert not pi.is_completed
    assert pi.root_execution.activity_id == "wait"
    (job,) = e1.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-catch" and job.node_id == "wait"
    assert job.duedate > fake_clock.now()  # 尚未到期
    # 未到期执行不触发
    assert e1.execute_due_jobs() == 0
    assert not e1.get_process_instance(pi.id).is_completed
    # 拨过 duedate -> 触发 -> 流程完成、作业清空
    fake_clock.advance(6)
    assert e1.execute_due_jobs() == 1
    assert e1.get_process_instance(pi.id).is_completed
    assert e1.create_job_query() == []
    # actinst：wait 已结算
    wait_ai = [
        a
        for a in e1.get_process_instance(pi.id).activity_history
        if a.activity_id == "wait"
    ]
    assert len(wait_ai) == 1 and wait_ai[0].end_time is not None


def test_timer_date_catch_absolute(fake_clock):
    """timeDate 绝对点：duedate 即文本解析出的本地绝对时刻，到点触发。"""
    e1 = ProcessEngine()
    deploy(e1, catch_flow("date-catch", "date", "2099-01-01T00:00:00"), "date-catch")
    pi = e1.start_process_instance_by_key("date-catch")
    (job,) = e1.create_job_query(process_instance_id=pi.id)
    assert job.duedate == "2099-01-01T00:00:00"
    assert e1.execute_due_jobs() == 0  # 还没到 2099
    # 拨到触发点之后
    jump = (datetime(2099, 1, 1) - fake_clock.t).total_seconds() + 1
    fake_clock.advance(jump)
    assert e1.execute_due_jobs() == 1
    assert e1.get_process_instance(pi.id).is_completed


def test_async_before_splits_service_task(fake_clock):
    """asyncBefore serviceTask：start 只开 actinst + async job，delegate 延后到 job 执行。"""
    calls: list[int] = []

    def delegate(vars_):
        calls.append(1)
        vars_["ran"] = True

    e1 = ProcessEngine()
    e1.register_delegate("heavy", delegate)
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "heavy-task")
        + e(
            "serviceTask",
            "heavy-task",
            'camunda:asyncBefore="true" camunda:delegateExpression="${heavy}"',
        )
        + f("f1", "heavy-task", "end")
        + e("endEvent", "end"),
        "async-before",
    )
    pi = e1.start_process_instance_by_key("async-before")
    # 拆分完成：delegate 未跑、actinst open、async job 就绪、token 停等
    assert calls == []
    assert not pi.is_completed
    assert pi.root_execution.activity_id == "heavy-task"
    assert pi.root_execution.open_activity is not None
    (job,) = e1.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "async-continuation" and job.node_id == "heavy-task"
    # async job duedate=now -> 执行 -> delegate 跑、流程完成
    assert e1.execute_due_jobs() == 1
    assert calls == [1]
    pi = e1.get_process_instance(pi.id)
    assert pi.is_completed and pi.variables.get("ran") is True
    assert e1.create_job_query() == []
    # actinst 仅一条（async 续跑复用 open actinst）
    ai = [a for a in pi.activity_history if a.activity_id == "heavy-task"]
    assert len(ai) == 1 and ai[0].end_time is not None


def test_failure_retries_then_succeeds(fake_clock):
    """async 执行失败：retries 减一、duedate 顺延；重试成功流程继续。"""
    attempts: list[int] = []

    def flaky(vars_):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom-first-time")

    e1 = ProcessEngine()
    e1.register_delegate("flaky", flaky)
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "flaky-task")
        + e(
            "serviceTask",
            "flaky-task",
            'camunda:asyncBefore="true" camunda:delegateExpression="${flaky}"',
        )
        + f("f1", "flaky-task", "end")
        + e("endEvent", "end"),
        "retry",
    )
    e1.start_process_instance_by_key("retry")
    # 第一次执行失败：job 仍在、retries 3->2、duedate 顺延
    e1.execute_due_jobs()
    (job,) = e1.create_job_query()
    assert job.retries == DEFAULT_MAX_RETRIES - 1
    assert job.duedate > fake_clock.now()
    assert len(attempts) == 1
    # 顺延窗口内不再尝试
    assert e1.execute_due_jobs() == 0 and len(attempts) == 1
    # 拨过顺延 -> 重试成功 -> 流程完成、job 删除
    fake_clock.advance(6)
    assert e1.execute_due_jobs() == 1
    assert len(attempts) == 2
    (pi,) = e1.list_process_instances()
    assert pi.is_completed
    assert e1.create_job_query() == []


def test_failure_exhausted_becomes_dead_letter(fake_clock):
    """一直失败：retries 耗尽 -> 死信（retries==0），不再被 acquire，记录保留。"""
    attempts: list[int] = []

    def always_fail(vars_):
        attempts.append(1)
        raise RuntimeError("always")

    e1 = ProcessEngine()
    e1.register_delegate("bad", always_fail)
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "bad-task")
        + e(
            "serviceTask",
            "bad-task",
            'camunda:asyncBefore="true" camunda:delegateExpression="${bad}"',
        )
        + f("f1", "bad-task", "end")
        + e("endEvent", "end"),
        "dead",
    )
    e1.start_process_instance_by_key("dead")
    for _ in range(DEFAULT_MAX_RETRIES):
        fake_clock.advance(6)
        e1.execute_due_jobs()
    (job,) = e1.create_job_query()
    assert job.retries == 0 and job.is_dead()
    assert len(attempts) == DEFAULT_MAX_RETRIES
    # 死信不再执行
    fake_clock.advance(60)
    assert e1.execute_due_jobs() == 0
    assert len(attempts) == DEFAULT_MAX_RETRIES
    # delete_job 可清理死信
    e1.delete_job(job.id)
    assert e1.create_job_query() == []
    with pytest.raises(NotFoundException):
        e1.delete_job(job.id)


def test_timer_start_duration_launches_instance(fake_clock):
    """timer-start(duration)：部署注册定义级 job，到点自动启动实例，一次性后删除。"""
    e1 = ProcessEngine()
    deploy(
        e1,
        e("startEvent", "s0", "", timer_evt("duration", "PT10S"))
        + f("f0", "s0", "t1")
        + e("userTask", "t1", 'name="after-start"'),
        "timer-start-dur",
    )
    (job,) = e1.create_job_query()
    assert job.job_type == "timer-start" and job.is_definition_level
    assert job.node_id == "s0" and job.process_definition_key == "timer-start-dur"
    assert e1.list_process_instances() == []
    # 定时启动流程不可手动启动（对齐 Camunda）
    with pytest.raises(Exception):
        e1.start_process_instance_by_key("timer-start-dur")
    # 未到期不触发
    fake_clock.advance(9)
    assert e1.execute_due_jobs() == 0 and e1.list_process_instances() == []
    # 到点触发 -> 实例启动停在 userTask
    fake_clock.advance(2)
    assert e1.execute_due_jobs() == 1
    (pi,) = e1.list_process_instances()
    assert not pi.is_completed and len(e1.create_task_query()) == 1
    assert e1.create_job_query() == []  # 一次性触发后删除


def test_timer_start_cycle_iso_repeat(fake_clock):
    """timer-start cycle ISO R3/PT10S：每 10s 触发一次，共 3 次后停排。"""
    e1 = ProcessEngine()
    deploy(
        e1,
        e("startEvent", "s0", "", timer_evt("cycle", "R3/PT10S"))
        + f("f0", "s0", "end")
        + e("endEvent", "end"),
        "cycle-iso",
    )
    (job,) = e1.create_job_query()
    assert job.repeat == {"kind": "interval", "seconds": 10.0, "count": 3}
    planned = job.duedate
    for i in range(1, 4):
        fake_clock.advance(11)
        assert e1.execute_due_jobs() == 1, f"第 {i} 次触发"
        assert len(e1.list_process_instances()) == i
        if i < 3:  # interval 按计划链续排 +10s，不漂移
            (job,) = e1.create_job_query()
            expected = format(  # 计划 duedate + 周期
                __import__("datetime").datetime.strptime(planned, "%Y-%m-%dT%H:%M:%S")
                + __import__("datetime").timedelta(seconds=10),
                "%Y-%m-%dT%H:%M:%S",
            )
            assert job.duedate == expected
            planned = job.duedate
    # count 耗尽 -> 作业删除，不再触发
    assert e1.create_job_query() == []
    fake_clock.advance(30)
    assert e1.execute_due_jobs() == 0
    assert len(e1.list_process_instances()) == 3


def test_timer_start_cycle_cron(fake_clock):
    """timer-start cycle cron：duedate 取下一 cron 触发点；触发后按表达式续排。"""
    fc = FakeClock(datetime(2026, 9, 2, 10, 0, 0))  # 固定起点 10:00
    clock.set_clock(fc.now)
    e1 = ProcessEngine()
    deploy(
        e1,
        e("startEvent", "s0", "", timer_evt("cycle", "0 5 * * *"))
        + f("f0", "s0", "end")
        + e("endEvent", "end"),
        "cycle-cron",
    )
    (job,) = e1.create_job_query()
    assert job.repeat == {"kind": "cron", "expr": "0 5 * * *"}
    assert job.duedate == "2026-09-03T05:00:00"  # 每日 05:00 -> 下一触发
    # 拨过触发点 -> 启动实例 -> cron 无限续排
    fc.advance(24 * 3600 + 60)
    assert e1.execute_due_jobs() == 1
    (job,) = e1.create_job_query()
    assert job.duedate == "2026-09-04T05:00:00"


def test_timer_start_flow_with_inner_timer_catch(fake_clock):
    """timer-start 触发 -> userTask -> timer catch 停等 -> 到期完成（全链路）。"""
    e1 = ProcessEngine()
    deploy(
        e1,
        e("startEvent", "s0", "", timer_evt("duration", "PT10S"))
        + f("f0", "s0", "t1")
        + e("userTask", "t1", 'name="step1"')
        + f("f1", "t1", "w1")
        + e("intermediateCatchEvent", "w1", "", timer_evt("duration", "PT5S"))
        + f("f2", "w1", "end")
        + e("endEvent", "end"),
        "start-then-catch",
    )
    # 到点触发 timer-start -> 实例启动停在 t1
    fake_clock.advance(11)
    assert e1.execute_due_jobs() == 1
    (pi,) = e1.list_process_instances()
    (task,) = e1.create_task_query()
    # 完成 t1 -> token 到 w1 -> 注册 timer-catch job 停等
    e1.complete_task(task.id, {"k": "v"})
    (job,) = e1.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-catch" and job.node_id == "w1"
    # 到期触发 -> 流程完成
    fake_clock.advance(6)
    assert e1.execute_due_jobs() == 1
    assert e1.get_process_instance(pi.id).is_completed
    assert e1.create_job_query() == []


def test_documented_differences_guard():
    """文档化差异守卫：catch 用 timerCycle / userTask 声明 asyncAfter -> 运行时明确报错。"""
    e1 = ProcessEngine()
    deploy(e1, catch_flow("catch-cycle", "cycle", "R/PT10S"), "catch-cycle")
    with pytest.raises(InvalidRequestException, match="timerCycle"):
        e1.start_process_instance_by_key("catch-cycle")
    # userTask asyncAfter：M4-1 仅支持 serviceTask / exclusiveGateway，其余类型明确报错
    deploy(
        e1,
        e("startEvent", "start")
        + f("f0", "start", "ut")
        + e("userTask", "ut", 'camunda:asyncAfter="true"')
        + f("f1", "ut", "end")
        + e("endEvent", "end"),
        "async-after-ut",
    )
    with pytest.raises(InvalidRequestException, match="asyncAfter"):
        e1.start_process_instance_by_key("async-after-ut")
