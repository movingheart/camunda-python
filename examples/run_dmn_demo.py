"""M5 DMN 决策引擎演示（决策表求值 + hitPolicy + businessRuleTask 集成）。

用法：
    python examples/run_dmn_demo.py

演示内容：
    1. 直接求值：DmnEngine 对贷款等级决策表（UNIQUE hitPolicy）按输入求值，
       覆盖通配列 / 区间 / 逗号列表等 FEEL 子集
    2. 决策表联动：利率折扣（COLLECT+SUM 多行命中聚合）消费上一决策结果
    3. 业务规则任务集成：BPMN 流程中 businessRuleTask 调用决策表，
       决策结果驱动排他网关选路（C 级走人工复核，其余自动通过）

说明：脚本末尾断言全部通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camunda.dmn.engine import DmnEngine
from camunda.engine import ProcessEngine
from camunda.parser import parse_bpmn_file
from camunda.parser.dmn_parser import parse_dmn_file

EXAMPLES = Path(__file__).resolve().parent


def demo_direct_evaluation() -> None:
    """演示 1：DmnEngine 直接求值决策表（UNIQUE 单行命中）。"""
    print("=" * 66)
    print("演示 1：直接求值（贷款等级决策表 UNIQUE：金额 x 信用分）")
    print("=" * 66)
    engine = DmnEngine()
    engine.deploy(parse_dmn_file(str(EXAMPLES / "loan-grading.dmn")))
    print(f"已部署决策：loan-grading v{engine.get_decision_version('loan-grading')}")
    cases = [
        ({"amount": 3000, "credit_score": 600}, "A"),   # 小额，信用分通配
        ({"amount": 9000, "credit_score": 750}, "B"),   # 大额 + 高信用
        ({"amount": 9000, "credit_score": 500}, "C"),   # 大额 + 低信用
    ]
    for variables, expect in cases:
        got = engine.evaluate_decision("loan-grading", variables)
        assert got == expect
        print(f"    {variables} -> 等级 {got}")
    print()


def demo_chained_decisions() -> None:
    """演示 2：决策联动（COLLECT+SUM 聚合消费上一决策输出）。"""
    print("=" * 66)
    print("演示 2：决策联动（利率折扣 COLLECT+SUM：命中多行求和）")
    print("=" * 66)
    engine = DmnEngine()
    engine.deploy(parse_dmn_file(str(EXAMPLES / "loan-grading.dmn")))
    grade = engine.evaluate_decision("loan-grading", {"amount": 9000, "credit_score": 750})
    discounts = engine.evaluate_decision("rate-discount", {"grade": grade})
    assert grade == "B" and discounts == 0.5
    print(f"    金额 9000 / 信用 750 -> 等级 {grade}")
    print(f"    等级 {grade} 命中折扣规则（A,B=0.5 与 B 无叠加行 -> SUM = {discounts}）")
    grade_a = engine.evaluate_decision("loan-grading", {"amount": 1000, "credit_score": 800})
    discounts_a = engine.evaluate_decision("rate-discount", {"grade": grade_a})
    assert discounts_a == 0.8  # A 命中两行：0.5 + 0.3
    print(f"    等级 {grade_a} 命中两行（0.5 + 0.3）-> SUM = {discounts_a}")
    print()


def demo_business_rule_flow() -> None:
    """演示 3：businessRuleTask 集成（决策结果驱动网关选路）。"""
    print("=" * 66)
    print("演示 3：业务规则任务集成（C 级走人工复核，其余自动通过）")
    print("=" * 66)
    engine = ProcessEngine()
    engine.deploy_dmn(parse_dmn_file(str(EXAMPLES / "loan-grading.dmn")))
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "loan-grading-flow.bpmn")))

    # C 级：停人工复核任务
    pi_c = engine.start_process_instance_by_key(
        "loan-process", {"amount": 20000, "credit_score": 500}
    )
    (task,) = engine.create_task_query(process_instance_id=pi_c.id)
    assert task.task_definition_key == "review"
    assert pi_c.variables["grade"] == "C"
    print(f"    20000/500 -> 等级 {pi_c.variables['grade']} -> 进入人工复核")
    engine.complete_task(task.id, {"approved": True})
    assert engine.get_process_instance(pi_c.id).is_completed
    print("    复核通过 -> 实例完成")

    # A 级：直接自动通过
    pi_a = engine.start_process_instance_by_key(
        "loan-process", {"amount": 2000, "credit_score": 800}
    )
    assert pi_a.is_completed
    assert pi_a.variables["grade"] == "A"
    assert pi_a.variables["result"] == 0.8  # COLLECT+SUM 聚合折扣
    print(f"    2000/800 -> 等级 {pi_a.variables['grade']}，折扣 {pi_a.variables['result']} -> 自动通过")
    assert engine.create_task_query() == []  # 复核任务随实例结束归档
    print()


if __name__ == "__main__":
    demo_direct_evaluation()
    demo_chained_decisions()
    demo_business_rule_flow()
    print("=" * 66)
    print("全部演示通过 ✅")
    print("=" * 66)
