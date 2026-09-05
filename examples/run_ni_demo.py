"""M4-2b4 非中断式边界事件（cancelActivity=false）演示（真实时钟 + JobExecutor）。

用法：
    python examples/run_ni_demo.py

演示内容（客服工单场景，真实耗时约 6 秒）：
    A. 正常完成：工单快速处理完毕 -> 非中断边界 timer 未到期、随宿主离开撤销，
       无并发线、无提醒。
    B. 超时催办：工单 2 秒无人处理 -> 边界到期触发并发催办线（处理任务仍在、
       不被打断）-> 提醒发出后收束 -> 处理人随后完成 -> 实例收束完成。
    C. 并发复核收束：2 秒后触发并发复核任务 -> 主线先完成、实例不提前结束
       （root 转 SCOPE 停驻等待）-> 复核完成后实例才收束完成。

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

EXAMPLES = Path(__file__).resolve().parent


def wait_until(pred, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时: {what}")


def new_engine() -> ProcessEngine:
    engine = ProcessEngine()

    def send_reminder(vars_):
        vars_["reminded"] = vars_.get("reminded", 0) + 1
        print(f"    [sendReminder] 工单 #{vars_.get('ticket', '?')} 已催办 "
              f"（第 {vars_['reminded']} 次提醒）")

    engine.register_delegate("sendReminder", send_reminder)
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "ticket-ni-support.bpmn")))
    return engine


def demo_fast_close() -> None:
    """演示 A：正常完成 —— 边界 timer 未到期，随宿主离开撤销，无并发线。"""
    print("=" * 66)
    print("演示 A：正常完成（2 秒内处理完毕 -> NI 边界撤销、无催办）")
    print("=" * 66)
    engine = new_engine()
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        pi = engine.start_process_instance_by_key("ticket-support", {"ticket": "T-1001"})
        wait_until(lambda: engine.create_task_query(process_instance_id=pi.id), 5, "处理任务")
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        (job,) = engine.create_job_query(process_instance_id=pi.id)
        assert job.job_type == "timer-boundary" and job.node_id == "remindEsc"
        print(f"工单 #{pi.variables['ticket']} 待处理，非中断边界 timer 已挂（due={job.duedate}）")
        # 立即处理（< 2s）：宿主正常离开 -> 边界 job 撤销
        engine.complete_task(task.id, {"resolution": "resolved"})
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 5, "流程完成")
        print("处理完成 -> 边界 timer 随宿主离开撤销 -> 无并发线触发")
        assert not pi.variables.get("reminded", False)  # 从未催办
        assert engine.create_job_query() == [] and engine.create_task_query() == []
        acts = {a.activity_id: a for a in pi.activity_history}
        assert "remindTask" not in acts and "handle" in acts
        print("    actinst 留痕：handle(结算) -> endDone（无 remindEsc/remindTask）")
    finally:
        ex.shutdown(timeout=1)
    print()


def demo_timeout_remind() -> None:
    """演示 B：超时催办 —— 边界触发并发催办线，宿主处理任务保留不被取消。"""
    print("=" * 66)
    print("演示 B：超时催办（2 秒无人处理 -> 并发催办线执行，宿主仍保留）")
    print("=" * 66)
    engine = new_engine()
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        pi = engine.start_process_instance_by_key("ticket-support", {"ticket": "T-2002"})
        wait_until(lambda: engine.create_task_query(process_instance_id=pi.id), 5, "处理任务")
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        print(f"工单 #{pi.variables['ticket']} 待处理（人工暂不处理，等待催办…）")
        # 2 秒后：NI 触发 —— 催办线执行；宿主任务仍在（cancelActivity=false）
        wait_until(lambda: pi.variables.get("reminded") == 1, 6, "催办触发")
        print("边界到期 -> 并发催办线已执行（宿主未取消！）")
        (task_after,) = engine.create_task_query(process_instance_id=pi.id)
        assert task_after.id == task.id  # 原处理任务仍在
        assert not engine.get_process_instance(pi.id).is_completed
        assert engine.create_job_query() == []  # NI timer 单发已消费
        # 催办提醒收束后，处理人完成工单 -> 实例收束完成
        engine.complete_task(task_after.id, {"resolution": "escalated"})
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 5, "流程完成")
        print("处理人完成工单 -> 实例收束完成（催办线早已收束）")
        assert pi.variables.get("reminded") == 1
        assert engine.create_task_query() == [] and engine.create_job_query() == []
        acts = {a.activity_id: a for a in pi.activity_history}
        assert "remindEsc" in acts and "remindTask" in acts
        assert acts["handle"].end_time is not None  # 宿主最终正常结算
        print("    actinst 留痕：handle(跨催办期正常结算) + remindEsc + remindTask -> endDone/endRemind")
    finally:
        ex.shutdown(timeout=1)
    print()


def demo_concurrent_review_waits() -> None:
    """演示 C：并发复核收束 —— 主线先完成，实例不提前结束，复核收束后才完成。"""
    print("=" * 66)
    print("演示 C：并发复核（主线先完不结束实例，并发复核线收束后才完成）")
    print("=" * 66)
    engine = new_engine()
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        pi = engine.start_process_instance_by_key("ticket-review", {"ticket": "T-3003"})
        wait_until(lambda: len(engine.create_task_query()) == 2, 6, "复核任务生成")
        tasks = {t.task_definition_key: t for t in engine.create_task_query()}
        assert set(tasks) == {"handle", "review"}
        print(f"工单 #{pi.variables['ticket']}：NI 已触发 -> 处理与复核双任务并存")
        # 主线 handle 先完成 -> 到 endMain：root 带活跃并发复核线 -> 转 SCOPE 停驻
        engine.complete_task(tasks["handle"].id, {"resolution": "resolved"})
        assert not engine.get_process_instance(pi.id).is_completed
        root = engine.get_process_instance(pi.id).root_execution
        assert root.role == "SCOPE" and root.activity_id is None
        print("主线完成 -> 并发复核未收束 -> 实例不结束（root 停驻等收束）")
        # 复核完成 -> 并发线收束 -> 实例完成
        engine.complete_task(tasks["review"].id, {"review": "ok"})
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 5, "流程完成")
        print("复核收束 -> 实例完成（主线 + 并发线全收束）")
        assert engine.create_task_query() == [] and engine.create_job_query() == []
        acts = {a.activity_id: a for a in pi.activity_history}
        assert "reviewEsc" in acts and "review" in acts
        assert "handle" in acts and "endMain" in acts
        print("    actinst 留痕：handle + reviewEsc + review -> endMain/endReview 全结算")
    finally:
        ex.shutdown(timeout=1)
    print()


if __name__ == "__main__":
    demo_fast_close()
    demo_timeout_remind()
    demo_concurrent_review_waits()
    print("=" * 66)
    print("全部演示通过 ✅  （真实时钟总耗时约 6 秒）")
    print("=" * 66)
