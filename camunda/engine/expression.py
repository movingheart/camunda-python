"""条件表达式安全求值（M1 安全子集）。

对齐 Camunda 语义：sequenceFlow 的 conditionExpression 形如
"${amount > 1000}" 或 "${approved == true}"。Camunda 用 JUEL/SpEL，
Python 侧没有直接等价物，M1 采用 **ast 白名单安全求值**：

支持：
- 字面量：数字 / 字符串 / true / false / null(->None)
- 变量引用：必须是流程变量（未定义抛 ProcessInstanceException，避免静默错误）
- 运算符：比较 == != < <= > >=，in / not in
- 逻辑：and / or / not（与 && / || / ! 做文本归一化）
- 算术：+ - * / %（数值）

不支持（M4 扩展）：方法调用、属性访问、日期时间函数、FEEL 语法。
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict

from camunda.common.exceptions import ProcessInstanceException

_ALLOWED_NODES = (
    ast.Expression,
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Name,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Load,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
)

_UNARY_TEXT = {"!": "not "}
# 兼容 JUEL/SpEL 风格逻辑符 -> Python
_TEXT_NORMALIZE = [
    ("&&", " and "),
    ("||", " or "),
    ("==", "=="),   # 占位保持通用
]


def _normalize(expr: str) -> str:
    """把常见 Java/JUEL 风格语法转成 Python 语法。

    注意顺序：先处理 && ||，再处理单字符 !（避免误伤 != / !in）。
    true/false/null 小写字面量在 Python 中不是关键字，需替换为大写形式。
    """
    out = expr
    for java_op, py_op in (("&&", " and "), ("||", " or ")):
        out = out.replace(java_op, py_op)
    # !x -> not x；需要避开 != 与 not in 场景（此处仅处理 ! 后跟空白/标识符/括号）
    out = re.sub(r"!(?=\s*\w|\s*\()", "not ", out)
    # 小写布尔/空字面量（词边界替换，避免误伤变量名如 "nullable"）
    out = re.sub(r"\btrue\b", "True", out)
    out = re.sub(r"\bfalse\b", "False", out)
    out = re.sub(r"\bnull\b", "None", out)
    return out


def evaluate_expression(expr_text: str, variables: Dict[str, Any]) -> Any:
    """求值表达式文本（可含 ${...} 包裹），返回 Python 值。"""
    expr = expr_text.strip()
    if expr.startswith("${") and expr.endswith("}"):
        expr = expr[2:-1].strip()
    if not expr:
        return True  # 空表达式视为无条件真（对齐 JUEL 对空条件行为）

    source = _normalize(expr)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise ProcessInstanceException(
            f"表达式语法错误 {expr_text!r}: {e}"
        ) from e

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ProcessInstanceException(
                f"表达式含不支持语法 {expr_text!r}: {type(node).__name__}"
            )

    # 变量解析：Name 必须是流程变量
    def _resolve(name: str) -> Any:
        # 内建布尔字面量在 3.8+ 是 Constant；Name 仅在变量表里查
        if name in variables:
            return variables[name]
        raise ProcessInstanceException(f"流程变量未定义: {name!r}（表达式 {expr_text!r}）")

    env = {name: _resolve(name) for name in set(
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    )}
    try:
        return eval(compile(tree, "<bpmn-expr>", "eval"), {"__builtins__": {}}, env)
    except ProcessInstanceException:
        raise
    except Exception as e:  # 运行时类型错误（如 str > int）
        raise ProcessInstanceException(
            f"表达式求值失败 {expr_text!r}: {e}"
        ) from e


def evaluate_condition(expr_text: str, variables: Dict[str, Any]) -> bool:
    """条件求值 -> bool（用于排他网关/条件流）。"""
    return bool(evaluate_expression(expr_text, variables))
