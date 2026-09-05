"""M7 多 JobExecutor 抢锁测试：DB CAS lease 原语 + 集成防双执行。

聚焦场景：
- store CAS 语义（acquire_due_jobs / complete_job_cas / reschedule_job_cas /
  extend_lock）：owner 校验 + lease 过期后可重新获取
- 两个 JobExecutor 共享同一 SQLite：同一 due job 只被其中一个执行
- 模拟崩溃：A 抢到 + 不主动释放 + 等过期 + B 重新抢到
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.common.timers import format_iso, parse_iso
from camunda.engine.process_engine import ProcessEngine
from camunda.job.executor import JobExecutor
from camunda.model.job import Job
from camunda.persistence.store import Store


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


def _store(tmp_path, name: str = "lock.db") -> Store:
    return Store(f"sqlite:///{tmp_path}/{name}")


def _job(
    jid: str,
    due: str,
    retries: int = 3,
    lock_owner: str | None = None,
    lock_exp: str | None = None,
) -> Job:
    """构造一个内存 Job（绕开 BPMN 直接落 DB 行）。"""
    return Job(
        id=jid,
        job_type="async-continuation",
        duedate=due,
        created=due,
        process_instance_id=None,
        execution_id=None,
        process_definition_key=None,
        node_id=None,
        retries=retries,
        lock_owner=lock_owner,
        lock_expire_time=lock_exp,
    )


# ----------------------------------------------------------------------
# store 层 CAS 单元（不依赖 engine）
# ----------------------------------------------------------------------
class TestStoreCAS:
    def test_acquire_only_one_owner_wins(self, tmp_path):
        """两个 owner 抢同一 due job：只有一个抢到。"""
        s = _store(tmp_path)
        now = clock.now()
        # 直接塞一个 due job 到 DB
        s.save_timer_start_jobs(
            [_job("j1", due=now, retries=3)]
        )
        a = s.acquire_due_jobs("owner-A", lease_seconds=60, due_before=now, batch_size=10)
        b = s.acquire_due_jobs("owner-B", lease_seconds=60, due_before=now, batch_size=10)
        assert len(a) == 1 and a[0].id == "j1" and a[0].lock_owner == "owner-A"
        assert b == []

    def test_complete_job_cas_owner_mismatch_no_op(self, tmp_path):
        s = _store(tmp_path)
        now = clock.now()
        s.save_timer_start_jobs([_job("j1", due=now)])
        s.acquire_due_jobs("owner-A", 60, now, 10)
        # owner-B 想删 -> 不删
        assert s.complete_job_cas("j1", "owner-B") is False
        # owner-A 想删 -> 删
        assert s.complete_job_cas("j1", "owner-A") is True
        assert s.acquire_due_jobs("owner-A", 60, now, 10) == []

    def test_reschedule_job_cas_owner_mismatch_no_op(self, tmp_path):
        s = _store(tmp_path)
        now = clock.now()
        s.save_timer_start_jobs([_job("j1", due=now, retries=3)])
        s.acquire_due_jobs("owner-A", 60, now, 10)
        # owner-B 想 reschedule -> 不改
        new_due = format_iso(parse_iso(now) + timedelta(seconds=3600))
        assert s.reschedule_job_cas("j1", "owner-B", new_due, 3) is False
        # owner-A 改 -> 成功，clear_lock 后另一 owner 可立刻抢到（但 duedate 已推到未来）
        assert s.reschedule_job_cas("j1", "owner-A", new_due, 3, clear_lock=True) is True
        # duedate 已推到 1h 后，没人会立刻再抢（虽然 LOCK 清了）
        assert s.acquire_due_jobs("owner-B", 60, now, 10) == []

    def test_extend_lock_owner_mismatch_no_op(self, tmp_path):
        s = _store(tmp_path)
        now = clock.now()
        s.save_timer_start_jobs([_job("j1", due=now)])
        s.acquire_due_jobs("owner-A", lease_seconds=1, due_before=now, batch_size=10)
        # owner-B 续约失败
        assert s.extend_lock("j1", "owner-B", 60, now) is False
        # owner-A 续约成功
        assert s.extend_lock("j1", "owner-A", 3600, now) is True

    def test_lease_expires_then_reacquirable(self, tmp_path, fake_clock):
        """lease=1s，等 2s 后另一 owner 可抢。"""
        s = _store(tmp_path)
        now = clock.now()
        s.save_timer_start_jobs([_job("j1", due=now)])
        a = s.acquire_due_jobs("owner-A", lease_seconds=1, due_before=now, batch_size=10)
        assert len(a) == 1
        # 立刻抢 B -> 抢不到（lease 未过期）
        assert s.acquire_due_jobs("owner-B", 60, now, 10) == []
        # 时钟拨 2s 后
        later = fake_clock.advance(2)
        b = s.acquire_due_jobs("owner-B", 60, due_before=later, batch_size=10)
        assert len(b) == 1 and b[0].lock_owner == "owner-B"

    def test_retries_zero_excluded_from_acquire(self, tmp_path):
        """retries=0（死信）不应被 acquire。"""
        s = _store(tmp_path)
        now = clock.now()
        s.save_timer_start_jobs([_job("j1", due=now, retries=0)])
        assert s.acquire_due_jobs("owner-A", 60, now, 10) == []

    def test_list_locks(self, tmp_path):
        s = _store(tmp_path)
        now = clock.now()
        s.save_timer_start_jobs([_job("j1", due=now), _job("j2", due=now)])
        s.acquire_due_jobs("owner-A", 60, now, 10)
        locks = s.list_locks()
        assert {l["id"] for l in locks} == {"j1", "j2"}
        assert all(l["lock_owner"] == "owner-A" for l in locks)


# ----------------------------------------------------------------------
# 集成：两个 JobExecutor 共享同一 store
# ----------------------------------------------------------------------
BPMN_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
BPMN_TAIL = "</bpmn:definitions>\n"


def _bpmn_timer_start(cycle: str = "R/PT1S") -> str:
    """部署一份 timer-start 周期作业（每 1s 触发一次）。"""
    return (
        BPMN_HEAD
        + '<bpmn:process id="tick" name="tick" isExecutable="true">'
        + '<bpmn:startEvent id="start">'
        + f'<bpmn:timerEventDefinition>'
        + f'<bpmn:timeCycle xsi:type="bpmn:tFormalExpression">{cycle}</bpmn:timeCycle>'
        + '</bpmn:timerEventDefinition>'
        + '</bpmn:startEvent>'
        + '<bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>'
        + '<bpmn:endEvent id="end"/>'
        + '</bpmn:process>'
        + BPMN_TAIL
    )


class TestTwoExecutors:
    def test_same_due_job_executed_by_only_one(self, tmp_path, fake_clock):
        """两个 JobExecutor 共享 SQLite：同一 due job 只被一个执行。

        共享内存实例池（单进程内两个 JobExecutor 都用同 engine + store）：
        任一执行后，另一个再 tick 时 due 列表已空（要么续排到未来、要么
        被 reschedule 时清 LOCK 后 duedate 已推到下次触发）。
        """
        from camunda.parser.bpmn_parser import parse_bpmn_xml

        store = _store(tmp_path)
        engine = ProcessEngine(store=store)
        engine.deploy(parse_bpmn_xml(_bpmn_timer_start("R/PT1S"), source_name="tick"))

        exec_a = JobExecutor(engine, name="exec-a", poll_interval=10)
        exec_b = JobExecutor(engine, name="exec-b", poll_interval=10)
        # 显式让两个 owner 都用可识别的字符串
        assert exec_a.lock_owner != exec_b.lock_owner
        assert exec_a.db_locking_enabled and exec_b.db_locking_enabled

        # 拨快 1s 让 timer-start due（R/PT1S 首次触发 = deploy 时 + 1s）
        fake_clock.advance(1)
        # 触发第一轮：A 抢到 + 执行（创建实例）
        n_a = exec_a.tick()
        # 此时 DB 中的 timer-start job 已被 reschedule（duedate 推到下次 + LOCK 清）
        # B 再 tick：候选 ID 列表中 duedate 已推到未来（1s 后），现在不 due
        n_b = exec_b.tick()

        # 只有 A 抢到这一轮（执行 1 条 = 启动 1 个流程实例）
        assert n_a >= 1
        assert n_b == 0
        # 验证 DB 中至少启动了一个实例
        assert len(engine.list_process_instances()) == 1

    def test_lease_handoff_after_owner_idle(self, tmp_path, fake_clock):
        """A 抢到后不释放（模拟崩溃），等 lease 过期 B 可重新抢。

        场景：直接塞一个 due 的定义级 job 让 A 持有 lease；B 等 lease 过期
        后能抢到。
        """
        store = _store(tmp_path)
        now = clock.now()
        # A 抢到并保留 lock（未执行 reschedule）
        store.save_timer_start_jobs([_job("j1", due=now)])
        got = store.acquire_due_jobs("owner-A", lease_seconds=1, due_before=now, batch_size=10)
        assert len(got) == 1
        # 时钟拨 2s（lease 已过期）
        later = fake_clock.advance(2)
        # B 抢
        got_b = store.acquire_due_jobs("owner-B", lease_seconds=60, due_before=later, batch_size=10)
        assert len(got_b) == 1
        assert got_b[0].lock_owner == "owner-B"

    def test_lock_owner_visible_in_list(self, tmp_path):
        """JobExecutor.lock_owner 可读，便于运维 / REST 暴露。"""
        store = _store(tmp_path)
        engine = ProcessEngine(store=store)
        exec = JobExecutor(engine, name="visible")
        # owner 形如 "name-pid-hostname-uuid8"：name 在最前（hostname 可能含连字符）
        parts = exec.lock_owner.split("-")
        assert parts[0] == "visible"
        assert len(parts[-1]) == 8  # uuid8 末尾
        assert len(parts) >= 4