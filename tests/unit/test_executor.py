"""JobExecutor 轮询线程测试：后台自动触发到期作业、shutdown 停表。

轮询用真实线程（poll 间隔很小），断言用条件等待避免 flaky。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.job import JobExecutor
from camunda.parser.bpmn_parser import parse_bpmn_xml

BPMN_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
BPMN_TAIL = "</bpmn:definitions>\n"


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime.now().replace(microsecond=0)

    def now(self) -> str:
        return self.t.strftime("%Y-%m-%dT%H:%M:%S")

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


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


def wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_executor_polls_and_fires_due_jobs(fake_clock, tmp_path):
    """后台轮询：timer-start 到期被自动触发 -> 实例启动停在 userTask -> shutdown 停表。"""
    from camunda.engine.process_engine import ProcessEngine

    e1 = ProcessEngine()
    xml = (
        BPMN_HEAD
        + '<bpmn:process id="auto-start" name="auto" isExecutable="true">'
        + e("startEvent", "s0", "", timer_evt("duration", "PT2S"))
        + f("f0", "s0", "t1")
        + e("userTask", "t1", 'name="human-step"')
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert "auto-start" in e1.deploy(parse_bpmn_xml(xml))
    ex = JobExecutor(e1, poll_interval=0.02)
    ex.start()
    try:
        # 未到期：轮询若干轮也不启动实例
        assert wait_until(lambda: len(e1.list_process_instances()) == 1, timeout=0.4) is False
        # 拨过到期点 -> 后台线程在 ~20ms 内自动触发
        fake_clock.advance(3)
        assert wait_until(lambda: len(e1.list_process_instances()) == 1)
        (pi,) = e1.list_process_instances()
        assert len(e1.create_task_query(process_instance_id=pi.id)) == 1
    finally:
        ex.shutdown(timeout=1.0)
    # shutdown 后不再有后台执行
    assert not ex.is_running
    assert ex.tick() == 0  # 手动 tick 仍可用


def test_executor_retries_failed_job_in_background(fake_clock):
    """后台轮询：async delegate 首次失败 -> 顺延后自动重试成功 -> 流程完成。"""
    from camunda.engine.process_engine import ProcessEngine

    attempts: list[int] = []

    def flaky(vars_):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first-fails")

    e1 = ProcessEngine()
    e1.register_delegate("flaky", flaky)
    xml = (
        BPMN_HEAD
        + '<bpmn:process id="auto-retry" name="retry" isExecutable="true">'
        + e("startEvent", "start")
        + f("f0", "start", "ft")
        + e(
            "serviceTask",
            "ft",
            'camunda:asyncBefore="true" camunda:delegateExpression="${flaky}"',
        )
        + f("f1", "ft", "end")
        + e("endEvent", "end")
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert "auto-retry" in e1.deploy(parse_bpmn_xml(xml))
    pi = e1.start_process_instance_by_key("auto-retry")
    # 缩短重试窗口便于后台观察（默认 5s）
    for job in e1.create_job_query():
        job.retry_delay_seconds = 1.0
    ex = JobExecutor(e1, poll_interval=0.02)
    ex.start()
    try:
        assert wait_until(lambda: len(attempts) >= 1)  # 首次失败已被捕获
        # 等 degrade 完成（retries 已减、duedate 已按当前时钟顺延）再拨钟，避免竞态
        assert wait_until(
            lambda: [j for j in e1.create_job_query() if j.retries == 2]
        )
        fake_clock.advance(2)  # 越过顺延窗口 -> 重试成功
        assert wait_until(lambda: e1.get_process_instance(pi.id).is_completed)
        assert len(attempts) == 2
        assert e1.create_job_query() == []
    finally:
        ex.shutdown(timeout=1.0)
