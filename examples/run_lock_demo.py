"""M7 多 JobExecutor 抢锁 demo：两个 JobExecutor 共享同一 SQLite，
观察同一 due job 只被其中一个抢到 + 执行（同步驱动：手工调 tick）。

场景：timer-start 周期 R/PT1S。两个 JobExecutor 同时轮询：
- A.tick() 抢到 due job -> 启动流程实例 + reschedule 清 LOCK + duedate 推到下次
- B.tick() 看到 LOCK 清 + duedate 已到未来 -> 抢不到本轮作业
- 重复几轮可观察 owner 分布

演示目的：
1. 验证 DB CAS lease 抢锁语义（两 executor 不会重复执行同一作业）
2. 验证 lease 过期可被另一 owner 接管（模拟崩溃恢复）
3. 验证 list_locks 监控接口
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 让 demo 直接 `python examples/run_lock_demo.py` 也能找到包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camunda.common.clock import now
from camunda.engine.process_engine import ProcessEngine
from camunda.job.executor import JobExecutor
from camunda.model.job import Job
from camunda.parser.bpmn_parser import parse_bpmn_xml
from camunda.persistence.store import Store


BPMN = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    '<bpmn:process id="tick" name="tick" isExecutable="true">'
    '<bpmn:startEvent id="start">'
    '<bpmn:timerEventDefinition>'
    '<bpmn:timeCycle xsi:type="bpmn:tFormalExpression">R/PT1S</bpmn:timeCycle>'
    '</bpmn:timerEventDefinition>'
    '</bpmn:startEvent>'
    '<bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="end"/>'
    '<bpmn:endEvent id="end"/>'
    '</bpmn:process>'
    '</bpmn:definitions>\n'
)


def main() -> None:
    print("=" * 60)
    print("M7 多 JobExecutor 抢锁 demo（同步驱动）")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="camunda-m7-"))
    db = tmp / "lock.db"
    store = Store(f"sqlite:///{db}")
    engine = ProcessEngine(store=store)
    engine.deploy(parse_bpmn_xml(BPMN, source_name="tick"))

    print(f"\n数据库: {db}")
    print(f"进程内 JobExecutor 数: 2（A + B），共享同一 engine + store")

    exec_a = JobExecutor(engine, name="exec-a", lease_seconds=60)
    exec_b = JobExecutor(engine, name="exec-b", lease_seconds=60)
    print(f"  owner A = {exec_a.lock_owner}")
    print(f"  owner B = {exec_b.lock_owner}")

    print("\n--- 场景 1：5 轮同步 tick（A、B 各一轮） ---")
    print("（每次 tick 后都用 store.list_locks() 观察锁分布）")
    print("（R/PT1S 首次 due = deploy + 1s，所以前 1s 内不 due；用真实 sleep 让时钟推进）")

    import time
    total_a = total_b = 0
    for i in range(5):
        n_a = exec_a.tick()
        n_b = exec_b.tick()
        total_a += n_a
        total_b += n_b
        locks = store.list_locks()
        active = [l for l in locks if l["lock_owner"] in (exec_a.lock_owner, exec_b.lock_owner)]
        print(
            f"  第 {i+1} 轮: A 执行 {n_a} 条, B 执行 {n_b} 条"
            f"  | 当前 A/B 持锁 {len(active)} 条"
        )
        # 每轮等 1.2s 让下一次 due
        time.sleep(1.2)

    print(f"\n5 轮累计：A 执行 {total_a} 条，B 执行 {total_b} 条")
    print(f"已启动流程实例数: {len(engine.list_process_instances())}")

    print("\n--- 场景 2：lease 过期可被另一 owner 接管 ---")
    # 直接塞一个 due job 让 A 持有短 lease，演示过期
    print(f"塞一个 due=now 的 job 到 ACT_RU_JOB，A 用 lease=1s 抢到")
    jid = "demo-j1"
    from camunda.model.job import Job
    now_str = now()
    store.save_timer_start_jobs(
        [Job(id=jid, job_type="async-continuation", duedate=now_str, created=now_str)]
    )
    got = store.acquire_due_jobs("demo-holder", lease_seconds=1, due_before=now_str, batch_size=10)
    print(f"  demo-holder 抢到 {len(got)} 条（LOCK 持有中）")
    print(f"  list_locks() -> {store.list_locks()}")

    # 时钟拨 2s（lease 已过期）；用真实的 time 拨
    import time
    print("  ...等待 2s（lease=1s 过期）...")
    time.sleep(2)
    got2 = store.acquire_due_jobs("demo-receiver", lease_seconds=60, due_before=now(), batch_size=10)
    print(f"  demo-receiver 在 lease 过期后抢到 {len(got2)} 条")
    print(f"  list_locks() -> {store.list_locks()}")

    print("\n" + "=" * 60)
    print("Demo 完成 ✅")
    print(f"DB 留作检查: {db}")
    print("=" * 60)


if __name__ == "__main__":
    main()