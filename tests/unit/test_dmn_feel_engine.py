"""M5-2/5-3：FEEL 子集求值器 + 决策表 hitPolicy 引擎测试。"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import (
    ExpressionEvaluationException,
    NotFoundException,
)
from camunda.dmn.feel import eval_expression, eval_unary_test
from camunda.dmn.engine import DmnEngine
from camunda.model.dmn import (
    Decision,
    DecisionTable,
    DmnInput,
    DmnModel,
    DmnOutput,
    DmnRule,
)


# ---------------------------------------------------------------------------
# FEEL unaryTests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,value,expected",
    [
        ("<= 5000", 3000, True),
        ("<= 5000", 6000, False),
        ("(5000..20000]", 5000, False),   # 左开
        ("(5000..20000]", 20000, True),   # 右闭
        ("[5000..20000)", 5000, True),
        ("> 20000", 25000, True),
        ('!= "x"', "y", True),
        ('= "x"', "x", True),
        ('"A", "B"', "B", True),          # 逗号列表 = OR
        ('"A", "B"', "C", False),
        ("not(30000)", 25000, True),
        ("not(30000)", 30000, False),     # not(x) = != x
        ("true", True, True),
        ("3", 3, True),                   # 裸值 = 相等语义
        ("[1..5], [10..20]", 15, True),
        ("[1..5], [10..20]", 7, False),
        ("]1..5[", 1, False),             # DMN 反向标记：双开区间端点不含
        ("]1..5[", 3, True),
    ],
)
def test_unary_test_semantics(text, value, expected):
    assert eval_unary_test(text, value) is expected


def test_unary_test_wildcard():
    assert eval_unary_test(None, 12345) is True  # 通配恒命中


def test_unary_test_null_semantics():
    assert eval_unary_test("= null", None) is True
    assert eval_unary_test("= null", 0) is False
    assert eval_unary_test("!= null", 0) is True
    assert eval_unary_test("(1..2]", None) is False  # null 不落任何区间


def test_unary_test_type_error_raises():
    assert eval_unary_test("> 3", None) is False  # null 排序比较 = false（不报错）
    with pytest.raises(ExpressionEvaluationException):
        eval_unary_test("> 3", "abc")  # 数值与字符串不可排序比较


# ---------------------------------------------------------------------------
# FEEL expression
# ---------------------------------------------------------------------------
def test_expression_literals_and_variables():
    assert eval_expression('"A"', {}) == "A"
    assert eval_expression("42", {}) == 42
    assert eval_expression("1.5", {}) == 1.5
    assert eval_expression("true", {}) is True
    assert eval_expression("null", {}) is None
    assert eval_expression("price", {"price": 99}) == 99
    assert eval_expression("missing", {}) is None  # 缺变量 -> null（FEEL 语义）


def test_expression_arithmetic_precedence():
    assert eval_expression("1 + 2 * 3", {}) == 7
    assert eval_expression("(1 + 2) * 3", {}) == 9
    assert eval_expression("10 / 4", {}) == 2.5
    assert eval_expression("-5 + 2", {}) == -3
    assert eval_expression('"a" + "b"', {}) == "ab"


def test_expression_errors():
    with pytest.raises(ExpressionEvaluationException):
        eval_expression("1 / 0", {})  # 除零
    with pytest.raises(ExpressionEvaluationException):
        eval_expression('"a" * 2', {})  # 字符串不支持乘法
    with pytest.raises(ExpressionEvaluationException):
        eval_expression("upper(x)", {})  # 函数调用不支持
    with pytest.raises(ExpressionEvaluationException):
        eval_expression("1 +", {})  # 语法不完整


# ---------------------------------------------------------------------------
# DmnEngine：hitPolicy 收敛
# ---------------------------------------------------------------------------
def make_decision(policy, rules, aggregator=None, output_values=None, outputs=None, key="d"):
    return Decision(
        id=key,
        decision_table=DecisionTable(
            hit_policy=policy,
            aggregator=aggregator,
            inputs=[DmnInput(id="i1", expression="amount")],
            outputs=outputs
            or [DmnOutput(id="o1", name="grade", output_values=output_values or [])],
            rules=rules,
        ),
    )


def deploy(engine, *decisions):
    engine.deploy(DmnModel(decisions=list(decisions)))
    return engine


def test_unique_hit_and_no_hit():
    e = deploy(
        DmnEngine(),
        make_decision("UNIQUE", [
            DmnRule(input_entries=["<= 5000"], output_entries=['"A"']),
            DmnRule(input_entries=["> 5000"], output_entries=['"B"']),
        ]),
    )
    assert e.evaluate_decision("d", {"amount": 3000}) == "A"
    assert e.evaluate_decision("d", {"amount": 9000}) == "B"
    assert e.evaluate_decision("d", {}) is None  # 输入缺失(null)无命中 -> 空结果


def test_unique_violation_raises():
    e = deploy(
        DmnEngine(),
        make_decision("UNIQUE", [
            DmnRule(input_entries=["> 0"], output_entries=["1"]),
            DmnRule(input_entries=["> 0"], output_entries=["2"]),
        ]),
    )
    with pytest.raises(ExpressionEvaluationException, match="UNIQUE 违例"):
        e.evaluate_decision("d", {"amount": 1})


def test_first_hit_picks_rule_order():
    e = deploy(
        DmnEngine(),
        make_decision("FIRST", [
            DmnRule(input_entries=["> 0"], output_entries=['"first"']),
            DmnRule(input_entries=["> 0"], output_entries=['"second"']),
        ]),
    )
    assert e.evaluate_decision("d", {"amount": 1}) == "first"


def test_any_consistent_and_violation():
    rules_ok = [
        DmnRule(input_entries=["> 0"], output_entries=['"same"']),
        DmnRule(input_entries=["< 100"], output_entries=['"same"']),
    ]
    assert deploy(DmnEngine(), make_decision("ANY", rules_ok)).evaluate_decision(
        "d", {"amount": 50}
    ) == "same"
    rules_bad = [
        DmnRule(input_entries=["> 0"], output_entries=['"x"']),
        DmnRule(input_entries=["< 100"], output_entries=['"y"']),
    ]
    e = deploy(DmnEngine(), make_decision("ANY", rules_bad))
    with pytest.raises(ExpressionEvaluationException, match="ANY 违例"):
        e.evaluate_decision("d", {"amount": 50})


def test_priority_by_output_values():
    e = deploy(
        DmnEngine(),
        make_decision(
            "PRIORITY",
            [
                DmnRule(input_entries=[None], output_entries=['"bronze"']),
                DmnRule(input_entries=[None], output_entries=['"gold"']),
            ],
            output_values=['"gold"', '"silver"', '"bronze"'],
        ),
    )
    assert e.evaluate_decision("d", {"amount": 1}) == "gold"


def test_priority_requires_single_output_and_values():
    two_cols = [
        DmnOutput(id="o1", name="g", output_values=['"a"']),
        DmnOutput(id="o2", name="h"),
    ]
    e = deploy(
        DmnEngine(),
        make_decision("PRIORITY", [DmnRule(input_entries=[None], output_entries=['"a"', None])], outputs=two_cols),
    )
    with pytest.raises(ExpressionEvaluationException, match="仅支持单输出列"):
        e.evaluate_decision("d", {"amount": 1})


def test_rule_order_returns_all_hits():
    e = deploy(
        DmnEngine(),
        make_decision("RULE ORDER", [
            DmnRule(input_entries=["> 0"], output_entries=["1"]),
            DmnRule(input_entries=["> 100"], output_entries=["2"]),
        ]),
    )
    assert e.evaluate_decision("d", {"amount": 500}) == [1, 2]


def test_rule_order_no_hit_empty_list():
    e = deploy(
        DmnEngine(),
        make_decision("RULE ORDER", [DmnRule(input_entries=["> 0"], output_entries=["1"])]),
    )
    assert e.evaluate_decision("d", {"amount": -1}) == []


def test_collect_aggregators():
    rules = [
        DmnRule(input_entries=["> 0"], output_entries=["10"]),
        DmnRule(input_entries=["> 100"], output_entries=["5"]),
    ]
    e = deploy(
        DmnEngine(),
        make_decision("COLLECT", rules, key="d"),
        make_decision("COLLECT", rules, aggregator="SUM", key="d1"),
        make_decision("COLLECT", rules, aggregator="MIN", key="d2"),
        make_decision("COLLECT", rules, aggregator="MAX", key="d3"),
        make_decision("COLLECT", rules, aggregator="COUNT", key="d4"),
    )
    assert e.evaluate_decision("d", {"amount": 500}) == [10, 5]
    assert e.evaluate_decision("d1", {"amount": 500}) == 15
    assert e.evaluate_decision("d2", {"amount": 500}) == 5
    assert e.evaluate_decision("d3", {"amount": 500}) == 10
    assert e.evaluate_decision("d4", {"amount": 500}) == 2


def test_collect_sum_requires_single_numeric_output():
    e = deploy(
        DmnEngine(),
        make_decision(
            "COLLECT",
            [DmnRule(input_entries=[None], output_entries=['"x"'])],
            aggregator="SUM",
        ),
    )
    with pytest.raises(ExpressionEvaluationException, match="要求数值输出"):
        e.evaluate_decision("d", {"amount": 1})


def test_multi_output_row_result_dict():
    e = deploy(
        DmnEngine(),
        Decision(
            id="d",
            decision_table=DecisionTable(
                hit_policy="FIRST",
                inputs=[DmnInput(id="i1", expression="amount")],
                outputs=[DmnOutput(id="o1", name="grade"), DmnOutput(id="o2", name="rate")],
                rules=[DmnRule(input_entries=[None], output_entries=['"A"', "0.5"])],
            ),
        ),
    )
    assert e.evaluate_decision("d", {"amount": 1}) == {"grade": "A", "rate": 0.5}


# ---------------------------------------------------------------------------
# 部署语义
# ---------------------------------------------------------------------------
def test_undeployed_decision_raises_not_found():
    with pytest.raises(NotFoundException, match="未部署的决策定义"):
        DmnEngine().evaluate_decision("nope", {})


def test_redeploy_bumps_version():
    e = deploy(DmnEngine(), make_decision("FIRST", []))
    assert e.get_decision_version("d") == 1
    e.deploy(DmnModel(decisions=[make_decision("FIRST", [])]))
    assert e.get_decision_version("d") == 2
