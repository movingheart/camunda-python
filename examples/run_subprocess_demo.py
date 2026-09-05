"""M4-2a embedded SubProcess 演示（真实时钟 + JobExecutor 后台轮询）。

用法：
    python examples/run_subprocess_demo.py

演示内容（订单履约：内嵌子流程 + 边界 timer 超时降级，真实耗时约 5 秒）：
    1. 正常履约：受理订单 -> 子流程（备货 -> 质检人工通过 -> 发货）-> 收束
       回主流程 -> 完成通知。边界 timer 未触发，随子流程正常离开撤销。
    2. 超时退款：质检任务 2 秒无人处理 -> 边界 timer 到期中断整段子流程 ->
       质检任务取消归档、子流程 actinst 结算 -> 自动退款路径完成。

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

JOB_KIND = {
    "timer-boundary": "边界 timer（子流程超时）",
    "async-after": "asyncAfter（离开推进）",
    "async-continuation": "asyncBefore（行为）",
    "timer-catch": "timer-catch",
    "timer-start": "timer-start",
}


def job_line(job) -> str:
    return (
        f"    job[{job.id[:8]}] {JOB_KIND.get(job.job_type, job.job_type)} "
        f"node={job.node_id} due={job.duedate} retries={job.retries}"
    )


def wait_until(pred, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时: {what}")


def new_engine() -> ProcessEngine:
    engine = ProcessEngine()

    def receive_order(vars_):
        print(f"    [receiveOrder] 订单 #{vars_.get('order', '?')} 已受理")

    def pack_items(vars_):
        vars_["itemsReady"] = True
        print(f"    [packItems]    订单 #{vars_.get('order', '?')} 备货完成")

    def ship_order(vars_):
        vars_["shipped"] = True
        print(f"    [shipOrder]    订单 #{vars_.get('order', '?')} 已发货")

    def notify_done(vars_):
        print(f"    [notifyDone]   订单 #{vars_.get('order', '?')} 履约完成")

    def auto_refund(vars_):
        vars_["refunded"] = True
        print(f"    [autoRefund]   订单 #{vars_.get('order', '?')} 超时已自动退款")

    engine.register_delegate("receiveOrder", receive_order)
    engine.register_delegate("packItems", pack_items)
    engine.register_delegate("shipOrder", ship_order)
    engine.register_delegate("notifyDone", notify_done)
    engine.register_delegate("autoRefund", auto_refund)
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "subprocess-dispatch.bpmn")))
    return engine


def demo_normal_fulfillment() -> None:
    """演示 1：正常履约 —— 质检人工通过 -> 内部走完 -> 子流程收束回主流程。"""
    print("=" * 66)
    print("演示 1：正常履约（子流程：备货 -> 质检通过 -> 发货 -> 收束完成）")
    print("=" * 66)
    engine = new_engine()
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        pi = engine.start_process_instance_by_key("order-fulfillment", {"order": "A-1001"})
        # 同步段（受理/备货）一次 pump 走完 -> 停在子流程内部质检任务
        wait_until(lambda: engine.create_task_query(process_instance_id=pi.id), 5, "质检任务")
        print("进入子流程：内部质检任务已生成（此刻 execution 树 = 子流程 SCOPE 停驻）")
        (job,) = engine.create_job_query(process_instance_id=pi.id)
        assert job.job_type == "timer-boundary"  # 子流程边界超时 timer
        print("子流程等待窗口的边界 timer：")
        print(job_line(job))
        assert pi.variables.get("itemsReady") is True  # 内部 delegate 已执行
        # 人工质检通过 -> 发货 -> 内部 end -> 子流程收束 -> 主流程完成通知
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        engine.complete_task(task.id, {"qc": "pass"})
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 5, "流程完成")
        print("质检通过 -> 内部走完 -> 子流程收束回主流程 -> 完成")
        assert pi.variables.get("shipped") is True
        assert not pi.variables.get("refunded", False)
        # 边界 timer 随子流程正常离开撤销
        assert engine.create_job_query() == []
        assert engine.create_task_query() == []
        acts = {a.activity_id: a for a in pi.activity_history}
        assert "fulfill" in acts and acts["fulfill"].end_time is not None
        assert "qualityCheck" in acts and "ship" in acts and "endDone" in acts
        print("    actinst 留痕：fulfill(结算) 覆盖内部 packItems/qualityCheck/ship -> endDone")
    finally:
        ex.shutdown(timeout=1)
    print()


def demo_timeout_refund() -> None:
    """演示 2：超时退款 —— 质检 2 秒无人处理，边界 timer 中断整段子流程。"""
    print("=" * 66)
    print("演示 2：超时退款（质检任务 2 秒无人处理 -> 中断整段子流程）")
    print("=" * 66)
    engine = new_engine()
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        pi = engine.start_process_instance_by_key("order-fulfillment", {"order": "B-2002"})
        wait_until(lambda: engine.create_task_query(process_instance_id=pi.id), 5, "质检任务")
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        (bd,) = engine.create_job_query(process_instance_id=pi.id)
        assert bd.job_type == "timer-boundary" and bd.node_id == "timeoutEsc"
        print("质检任务已生成（无人处理），子流程边界 timer 已挂：")
        print(job_line(bd))
        # 不 complete：2 秒后边界到期 -> JobExecutor 触发 scope 取消
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 6, "超时退款完成")
        print("边界 timer 到期 -> 整段子流程被中断 -> 自动退款完成")
        assert pi.variables.get("refunded") is True
        assert not pi.variables.get("shipped", False)  # 内部发货从未执行
        # 中断语义：质检任务归档、无泄漏 task/job、走了退款路径
        assert engine.create_task_query() == []
        assert engine.create_job_query() == []
        archived = next(t for t in pi.completed_tasks if t.id == task.id)
        assert archived.end_time is not None
        acts = {a.activity_id: a for a in pi.activity_history}
        assert acts["fulfill"].end_time is not None  # 子流程 actinst 结算
        assert "timeoutEsc" in acts and "autoRefund" in acts and "endRefund" in acts
        assert "endDone" not in acts and "ship" not in acts
        print("    actinst 留痕：fulfill(结算) -> timeoutEsc(边界) -> autoRefund -> endRefund")
    finally:
        ex.shutdown(timeout=1)
    print()


if __name__ == "__main__":
    demo_normal_fulfillment()
    demo_timeout_refund()
    print("=" * 66)
    print("全部演示通过 ✅  （真实时钟总耗时约 5 秒）")
    print("=" * 66)
