"""M3 作业/定时器演示（真实时钟 + JobExecutor 后台轮询 + 持久化崩溃恢复）。

用法：
    python examples/run_timer_demo.py

演示内容：
    1. 定时巡检：timer-start(PT2S) 自动启动 -> asyncBefore 扫描 -> 人工复核
       -> timer-catch(PT3S) 冷却 -> 完成（JobExecutor 全自动，~8 秒）
    2. 周期结算：timer-start cycle R3/PT2S 每 2 秒触发一次，共 3 次后停排（~7 秒）
    3. 崩溃恢复：实例停在 timer-catch/人工任务落库 -> from_database 重启 -> 恢复续跑

说明：演示用真实时钟，等待时间即真实秒数；脚本末尾断言全部通过。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camunda.engine import ProcessEngine
from camunda.job import JobExecutor
from camunda.parser import parse_bpmn_file
from camunda.persistence.store import Store

EXAMPLES = Path(__file__).resolve().parent


def wait_until(pred, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时: {what}")


def job_line(job) -> str:
    kind = {
        "timer-start": "定义级 timer-start",
        "timer-catch": "实例级 timer-catch",
        "async-continuation": "实例级 async",
    }.get(job.job_type, job.job_type)
    return f"    job[{job.id[:8]}] {kind} node={job.node_id} due={job.duedate} retries={job.retries}"


def demo_inspection() -> None:
    """演示 1：定时启动 + asyncBefore 拆分 + 人工任务 + timer-catch 冷却（全自动）。"""
    print("=" * 66)
    print("演示 1：定时巡检（timer-start 自动启动 -> async 扫描 -> 复核 -> 冷却到期）")
    print("=" * 66)
    engine = ProcessEngine()
    scan_count: list[int] = []

    def health_scan(vars_):
        scan_count.append(1)
        print(f"    [healthScan] 巡检 {vars_.get('host', 'web-01')} ... 状态正常")
        vars_["health"] = "ok"

    engine.register_delegate("healthScan", health_scan)
    model = parse_bpmn_file(str(EXAMPLES / "timer-inspection.bpmn"))
    engine.deploy(model)

    print("部署完成，当前作业池：")
    (start_job,) = engine.create_job_query()
    assert start_job.job_type == "timer-start"
    print(job_line(start_job))

    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        # 2 秒后 timer-start 到期 -> 自动启动实例 -> async 扫描 job 就绪
        wait_until(lambda: len(engine.list_process_instances()) == 1, 5, "实例启动")
        wait_until(lambda: scan_count, 5, "async 扫描执行")
        print(f"timer-start 已自动触发（扫描执行 {len(scan_count)} 次）")
        (pi,) = engine.list_process_instances()
        assert pi.variables.get("health") == "ok"
        wait_until(lambda: engine.create_task_query(process_instance_id=pi.id), 5, "复核任务")
        print("流程停在人工复核 -> async-continuation 作业已消费")
        # 人工复核完成 -> token 进入 cooldown（timer-catch job 3 秒）
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        engine.complete_task(task.id, {"remark": "all good"})
        (cool,) = engine.create_job_query(process_instance_id=pi.id)
        assert cool.job_type == "timer-catch" and cool.node_id == "cooldown"
        print("复核完成 -> 冷却等待，timer-catch 作业：")
        print(job_line(cool))
        # 3 秒后冷却到期 -> 流程自动完成
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 6, "流程完成")
        print("冷却到期 -> 流程实例已完成")
        assert engine.create_job_query() == []
        assert engine.create_task_query() == []
    finally:
        ex.shutdown(timeout=1)
    print()


def demo_billing() -> None:
    """演示 2：timer-start cycle（ISO R3/PT2S）周期结算，共 3 次后停排。"""
    print("=" * 66)
    print("演示 2：周期结算（timeCycle R3/PT2S：每 2 秒结算一次，共 3 次）")
    print("=" * 66)
    engine = ProcessEngine()
    charge_count: list[int] = []

    def charge(vars_):
        charge_count.append(1)
        print(f"    [charge] 第 {len(charge_count)} 次结算完成 (order={vars_.get('order', 'A-100')})")

    engine.register_delegate("charge", charge)
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "timer-billing.bpmn")))
    (cycle_job,) = engine.create_job_query()
    print(f"部署完成，周期作业 repeat={cycle_job.repeat}")
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        wait_until(lambda: len(charge_count) == 3, 12, "3 次周期结算")
        print(f"3 次结算全部触发（实例数 {len(engine.list_process_instances())}）")
        assert engine.create_job_query() == [], "R3 周期耗尽后作业应删除"
        print("周期作业已停排（R3 count 耗尽）")
    finally:
        ex.shutdown(timeout=1)
    print()


def demo_crash_recovery() -> None:
    """演示 3：持久化崩溃恢复 —— 定时流程中途停机，重启后作业/任务续跑。"""
    print("=" * 66)
    print("演示 3：崩溃恢复（timer-catch 停等落库 -> 重启 -> 到期续跑）")
    print("=" * 66)
    db = str(EXAMPLES / "timer-demo.db")
    if Path(db).exists():
        Path(db).unlink()

    # 第一次运行：部署 + 启动 -> 完成复核 -> 停在冷却等待（落库）
    engine1 = ProcessEngine(store=Store(db))
    engine1.register_delegate("healthScan", lambda v: v.update(health="ok"))
    engine1.deploy(parse_bpmn_file(str(EXAMPLES / "timer-inspection.bpmn")))
    ex1 = JobExecutor(engine1, poll_interval=0.2)
    ex1.start()
    try:
        wait_until(lambda: len(engine1.list_process_instances()) == 1, 5, "实例1 启动")
        wait_until(lambda: engine1.create_task_query(), 5, "复核任务")
        (task,) = engine1.create_task_query()
        engine1.complete_task(task.id)
        (cool,) = engine1.create_job_query()
        assert cool.job_type == "timer-catch"
        print(f"实例已停在冷却等待并落库，timer-catch job due={cool.duedate}")
        print("    >>> 模拟进程崩溃：引擎/执行器直接丢弃 <<<")
    finally:
        ex1.shutdown(timeout=1)

    # 第二次运行：从库恢复 -> 到期自动续跑
    engine2 = ProcessEngine.from_database(db)
    engine2.register_delegate("healthScan", lambda v: v.update(health="ok"))
    print("重启恢复：")
    (pi,) = engine2.list_process_instances()
    print(f"    实例 {pi.id} 状态={pi.state.value} 位置={pi.root_execution.activity_id}")
    (restored,) = engine2.create_job_query()
    print(f"    作业 {restored.id[:8]} due={restored.duedate}（剩余 {restored.retries} 次重试）")
    ex2 = JobExecutor(engine2, poll_interval=0.2)
    ex2.start()
    try:
        wait_until(lambda: engine2.get_process_instance(pi.id).is_completed, 6, "恢复后完成")
        print("    冷却到期 -> 恢复后的引擎上流程自动完成")
        assert engine2.create_job_query() == []
    finally:
        ex2.shutdown(timeout=1)
    assert Store(db).load_active_instances() == [], "RU 应随实例完成清空"
    Path(db).unlink()  # 清理演示库
    print()


if __name__ == "__main__":
    demo_inspection()
    demo_billing()
    demo_crash_recovery()
    print("=" * 66)
    print("全部演示通过 ✅  （真实时钟总耗时约 20 秒）")
    print("=" * 66)
