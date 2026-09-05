"""贷款审批 + 并行审批 演示脚本。

用法：
    python examples/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camunda.engine import ProcessEngine
from camunda.parser import parse_bpmn_file

EXAMPLES = Path(__file__).resolve().parent


def demo_loan_approval(engine: ProcessEngine) -> None:
    """演示 1：排他网关 + 服务任务 + 用户任务 + 变量驱动分支。"""
    print("=" * 60)
    print("演示 1：贷款审批（金额 >= 10000 走人工审批，否则自动通过）")
    print("=" * 60)
    model = parse_bpmn_file(str(EXAMPLES / "loan-approval.bpmn"))

    # 注册服务任务实现（对应 camunda:delegateExpression="${checkCredit}"）
    def check_credit(vars_):
        print(f"  [checkCredit] 校验客户 {vars_.get('applicant')} 信用额度...")
        vars_["credit_ok"] = True
        return None  # 原地修改

    engine.register_delegate("checkCredit", check_credit)
    engine.deploy(model, name="loan-approval")

    # 场景 A：小额 -> 自动通过
    pi_a = engine.start_process_instance_by_key(
        "loan-approval", {"applicant": "张三", "amount": 5000}
    )
    print(f"  场景A(5000元) -> 实例 {pi_a.id} 状态={pi_a.state.value}")
    assert pi_a.state.value == "COMPLETED", "小额应自动通过并结束"
    print(f"  变量: {pi_a.variables}")

    # 场景 B：大额 -> 人工审批
    pi_b = engine.start_process_instance_by_key(
        "loan-approval", {"applicant": "李四", "amount": 50000}
    )
    tasks = engine.create_task_query(process_instance_id=pi_b.id)
    print(f"  场景B(50000元) -> 实例 {pi_b.id} 待办任务: {[t.name for t in tasks]}")
    assert len(tasks) == 1 and tasks[0].name == "人工审批"

    # 经理同意
    engine.complete_task(tasks[0].id, {"approved": True})
    print(f"  经理同意后实例状态={pi_b.state.value}")
    assert pi_b.state.value == "COMPLETED"
    print(f"  活动历史: {[a.activity_id for a in pi_b.activity_history]}")
    print()


def demo_parallel(engine: ProcessEngine) -> None:
    """演示 2：并行网关 fork/join（两个待办并发完成）。"""
    print("=" * 60)
    print("演示 2：并行审批（fork 两个任务，join 汇聚后结束）")
    print("=" * 60)
    model = parse_bpmn_file(str(EXAMPLES / "parallel-review.bpmn"))
    engine.deploy(model, name="parallel-review")

    pi = engine.start_process_instance_by_key("parallel-review", {"subject": "年度预算"})
    tasks = engine.create_task_query(process_instance_id=pi.id)
    print(f"  启动后并行待办: {sorted(t.name for t in tasks)}")
    assert len(tasks) == 2, "并行网关应产生两个待办"

    # 先完成一个：流程应仍 ACTIVE（join 等待另一个）
    first, second = sorted(tasks, key=lambda t: t.id)
    engine.complete_task(first.id, {"a_ok": True})
    print(f"  完成 {first.name} 后实例状态={pi.state.value}（应仍 ACTIVE）")
    assert pi.state.value == "ACTIVE", "并行 join 未到齐不应结束"

    engine.complete_task(second.id, {"b_ok": True})
    print(f"  完成 {second.name} 后实例状态={pi.state.value}")
    assert pi.state.value == "COMPLETED", "两个任务都完成后应 COMPLETED"
    print(f"  活动历史: {[a.activity_id for a in pi.activity_history]}")
    print()


def main() -> None:
    engine = ProcessEngine()
    demo_loan_approval(engine)
    demo_parallel(engine)
    print("✅ 全部演示通过")


if __name__ == "__main__":
    main()
