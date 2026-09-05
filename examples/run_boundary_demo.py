"""M4-1 边界事件 / asyncAfter 演示（真实时钟 + JobExecutor 后台轮询）。

用法：
    python examples/run_boundary_demo.py

演示内容（审批超时自动降级，~8 秒）：
    1. 正常审批：发起 -> 提交审计(asyncAfter 拆离开 job 自动消费) -> 人工审批
       -> 通过归档 -> 完成（边界 timer 未触发即随宿主完成撤销）
    2. 超时降级：发起 -> 审批任务 2 秒无人处理 -> timer 边界到期中断宿主 -> 走
       超时升级路径完成（审批任务取消归档、边界 actinst 留痕）

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


JOB_KIND = {
    "timer-boundary": "边界 timer（中断）",
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


def new_engine() -> ProcessEngine:
    engine = ProcessEngine()

    def audit_submit(vars_):
        print(f"    [auditSubmit] 审批 #{vars_.get('req', '?')} 审计已登记")

    def archive(vars_):
        print(f"    [archive] 审批 #{vars_.get('req', '?')} 通过归档")

    def escalate(vars_):
        print(f"    [escalate] 审批 #{vars_.get('req', '?')} 超时 -> 已自动升级处理")
        vars_["escalated"] = True

    engine.register_delegate("auditSubmit", audit_submit)
    engine.register_delegate("archive", archive)
    engine.register_delegate("escalate", escalate)
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "boundary-approval.bpmn")))
    return engine


def demo_normal_approval() -> None:
    """演示 1：正常审批（2 秒内完成）—— asyncAfter 自动消费、边界随宿主撤销。"""
    print("=" * 66)
    print("演示 1：正常审批（asyncAfter 审计 job 自动消费 -> 人工通过 -> 边界撤销）")
    print("=" * 66)
    engine = new_engine()
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        pi = engine.start_process_instance_by_key("approval-timeout", {"req": "A-1"})
        # asyncAfter 拆出的离开 job 在启动后由 JobExecutor 自动消费 -> 审批任务出现
        wait_until(lambda: engine.create_task_query(process_instance_id=pi.id), 5, "审批任务")
        print("提交审计的 async-after 作业已自动消费，当前作业池：")
        for j in engine.create_job_query(process_instance_id=pi.id):
            assert j.job_type == "timer-boundary"  # 只剩审批超时边界
            print(job_line(j))
        # 立即审批通过 -> 主路归档完成；边界 timer 随宿主离开撤销
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        engine.complete_task(task.id, {"decision": "approved"})
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 5, "流程完成")
        print("审批通过 -> 主路完成；边界作业已撤销（未触发）")
        assert engine.create_job_query() == []
        assert engine.create_task_query() == []
        assert not pi.variables.get("escalated", False)
    finally:
        ex.shutdown(timeout=1)
    print()


def demo_timeout_escalation() -> None:
    """演示 2：超时降级 —— 审批 2 秒无人处理，边界 timer 到期中断宿主。"""
    print("=" * 66)
    print("演示 2：超时降级（审批任务 2 秒无人处理 -> 边界 timer 中断宿主）")
    print("=" * 66)
    engine = new_engine()
    ex = JobExecutor(engine, poll_interval=0.2)
    ex.start()
    try:
        pi = engine.start_process_instance_by_key("approval-timeout", {"req": "B-2"})
        wait_until(lambda: engine.create_task_query(process_instance_id=pi.id), 5, "审批任务")
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        print("审批任务已生成（无人处理）...")
        (bd,) = engine.create_job_query(process_instance_id=pi.id)
        assert bd.job_type == "timer-boundary"
        print(job_line(bd))
        # 不 complete：2 秒后边界到期 -> JobExecutor 触发中断 -> 超时升级路径
        wait_until(lambda: engine.get_process_instance(pi.id).is_completed, 6, "超时降级完成")
        print("边界 timer 到期 -> 审批被中断 -> 超时升级完成")
        assert pi.variables.get("escalated") is True
        assert engine.create_task_query() == []  # 审批任务已取消
        assert engine.create_job_query() == []
        # 留痕核对：审批任务归档带结束时间；宿主 actinst 结算；走了 timeout 路径
        archived = next(t for t in pi.completed_tasks if t.id == task.id)
        assert archived.end_time is not None
        acts = {a.activity_id: a for a in pi.activity_history}
        assert acts["approve"].end_time is not None
        assert "timeout" in acts and "autoEscalate" in acts and "endTimeout" in acts
        assert "endApproved" not in acts
        print("    actinst 留痕：approve(结算) -> timeout(边界) -> autoEscalate -> endTimeout")
    finally:
        ex.shutdown(timeout=1)
    print()


if __name__ == "__main__":
    demo_normal_approval()
    demo_timeout_escalation()
    print("=" * 66)
    print("全部演示通过 ✅  （真实时钟总耗时约 6 秒）")
    print("=" * 66)
