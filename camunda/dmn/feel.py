"""FEEL 表达式子集求值器（M5-2，手写递归下降，无外部依赖）。

范围（文档化差异：仅 FEEL Friendly 子集，覆盖决策表典型用法）：

unaryTests（输入单元格，配合输入值求布尔）：
- 通配（空文本/"-"，解析期已归一为 None）恒命中
- 布尔/数值/字符串字面量比较（裸值 = 相等语义）
- 比较算子：= != < <= > >=
- 区间：[a..b] 闭闭、(a..b) 开开、]a..b[ / (a..b] 混合开闭（DMN 双标记法均支持）
- 逗号列表 = OR（任一命中）
- not(...) 取反
- null 字面量（= null 判缺变量）

expression（输出单元格 / inputExpression）：
- 字面量：number / "string" / true / false / null
- 变量引用（IDENT，未定义 -> null，对齐 FEEL 缺变量语义）
- 算术：+ - * / 与一元负号、括号，标准优先级
- 字符串 + 拼接

不支持（运行时明确报错）：between、in、函数调用、日期时间、路径表达式、
instance of、for/some/every 等。
"""

from __future__ import annotations

from typing import Any, List, Optional

from camunda.common.exceptions import ExpressionEvaluationException

# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------
_TWO_CHAR_OPS = {">=", "<=", "!="}
_ONE_CHAR_OPS = set("><=+-*/(),[]")


class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int) -> None:
        self.kind = kind  # NUM / STR / IDENT / OP / DOTDOT / EOF
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # 调试便利
        return f"_Token({self.kind!r}, {self.value!r})"


def _tokenize(text: str) -> List[_Token]:
    tokens: List[_Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ExpressionEvaluationException(
                    f"字符串字面量未闭合: {text!r}"
                )
            tokens.append(_Token("STR", "".join(buf), i))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            seen_dot = False
            while j < n and (text[j].isdigit() or (text[j] == "." and not seen_dot)):
                if text[j] == ".":
                    # ".." 是区间分隔符，不是小数点
                    if j + 1 < n and text[j + 1] == ".":
                        break
                    seen_dot = True
                j += 1
            raw = text[i:j]
            num = float(raw) if "." in raw else int(raw)
            tokens.append(_Token("NUM", num, i))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(_Token("IDENT", text[i:j], i))
            i = j
            continue
        if text.startswith("..", i):
            tokens.append(_Token("DOTDOT", "..", i))
            i += 2
            continue
        if text[i : i + 2] in _TWO_CHAR_OPS:
            tokens.append(_Token("OP", text[i : i + 2], i))
            i += 2
            continue
        if ch in _ONE_CHAR_OPS:
            tokens.append(_Token("OP", ch, i))
            i += 1
            continue
        raise ExpressionEvaluationException(
            f"FEEL 不支持的字符 {ch!r}（位置 {i}）: {text!r}"
        )
    tokens.append(_Token("EOF", None, n))
    return tokens


# ---------------------------------------------------------------------------
# 求值辅助
# ---------------------------------------------------------------------------
def _cmp_key(v: Any, ctx: str) -> Any:
    """比较前类型守卫：None 与不可比较类型直接报错（FEEL 非法比较语义）。"""
    if v is None:
        raise ExpressionEvaluationException(f"{ctx}: 操作数为 null 不可比较")
    if isinstance(v, bool) or isinstance(v, (int, float, str)):
        return v
    raise ExpressionEvaluationException(f"{ctx}: 类型 {type(v).__name__} 不可比较")


def _values_equal(a: Any, b: Any) -> bool:
    """FEEL 相等：数值跨 int/float；None 只与 null 相等；bool 严格。"""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return False


def _order_cmp(a: Any, b: Any) -> int:
    """FEEL 排序比较：数值或字符串同类；bool 不可排序比较。"""
    if isinstance(a, bool) or isinstance(b, bool):
        raise ExpressionEvaluationException("布尔值不支持 < <= > >= 比较")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a > b) - (a < b)
    if isinstance(a, str) and isinstance(b, str):
        return (a > b) - (a < b)
    raise ExpressionEvaluationException(
        f"类型不可排序比较: {type(a).__name__} vs {type(b).__name__}"
    )


# ---------------------------------------------------------------------------
# 递归下降解析/求值（parse 即 eval，无 AST 中间层——表达式足够小）
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, text: str, variables: dict) -> None:
        self.text = text
        self.vars = variables
        self.tokens = _tokenize(text)
        self.i = 0

    # -- token 工具 -----------------------------------------------------
    @property
    def cur(self) -> _Token:
        return self.tokens[self.i]

    def _next(self) -> _Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _expect_op(self, op: str) -> None:
        tok = self._next()
        if tok.kind not in ("OP", "DOTDOT") or tok.value != op:
            raise ExpressionEvaluationException(
                f"期望 {op!r} 实得 {tok.value!r}（位置 {tok.pos}）: {self.text!r}"
            )

    def _peek_is_op(self, *ops: str) -> Optional[str]:
        tok = self.cur
        if tok.kind == "OP" and tok.value in ops:
            return tok.value
        return None

    # -- 入口 -----------------------------------------------------------
    def eval(self) -> Any:
        val = self.additive()
        if self.cur.kind != "EOF":
            raise ExpressionEvaluationException(
                f"表达式存在未消费的尾部 {self.cur.value!r}: {self.text!r}"
            )
        return val

    def eval_unary_test(self, input_value: Any) -> bool:
        """unaryTests 入口：not(...) / 逗号 OR 列表 / 主体。"""
        return self._disjunction(input_value)

    def _next_is_open_paren(self) -> bool:
        nxt = self.tokens[self.i + 1] if self.i + 1 < len(self.tokens) else None
        return nxt is not None and nxt.kind == "OP" and nxt.value == "("

    def _disjunction(self, input_value: Any) -> bool:
        """逗号列表 = OR（DMN unaryTests 语义，逐项完整解析）。"""
        result = self._item(input_value)
        while self._peek_is_op(","):
            self._next()
            result = self._item(input_value) or result
        return result

    def _item(self, input_value: Any) -> bool:
        """单个 unaryTest 项：not(...) 或 positiveUnaryTest。"""
        if (
            self.cur.kind == "IDENT"
            and self.cur.value == "not"
            and self._next_is_open_paren()
        ):
            self._next()  # not
            self._expect_op("(")
            result = not self._disjunction(input_value)
            self._expect_op(")")
            return result
        return self._positive(input_value)

    def _positive(self, input_value: Any) -> bool:
        """positiveUnaryTest：比较 / 区间 / 裸表达式相等。"""
        op = self._peek_is_op("=", "!=", ">", ">=", "<", "<=")
        if op:
            self._next()
            if op in (">", ">=", "<", "<=") and input_value is None:
                return False  # FEEL：null 参与排序比较 = false（规则不命中）
            rhs = self.additive()
            return _apply_comparison(op, input_value, rhs)
        # 区间：[ ] ( 开头的 interval
        if self._peek_is_op("[", "]", "("):
            return self._interval(input_value)
        # 裸表达式 = 相等语义（FEEL：positiveUnaryExpression 命中判断）
        lhs = self.additive()
        return _values_equal(input_value, lhs)

    def _interval(self, input_value: Any) -> bool:
        open_tok = self._next()  # [ ] (
        open_ch = open_tok.value
        low_closed = open_ch == "["
        low = self.additive()
        self._expect_op("..")
        high = self.additive()
        close_tok = self._next()
        close_ch = close_tok.value
        if close_ch not in ("]", "[", ")"):
            raise ExpressionEvaluationException(
                f"区间右端非法 {close_ch!r}: {self.text!r}"
            )
        high_closed = close_ch == "]"
        if input_value is None:
            return False  # null 不落在任何区间
        try:
            above_low = _order_cmp(_cmp_key(input_value, "区间下界"), _cmp_key(low, "区间下界"))
            above_high = _order_cmp(_cmp_key(input_value, "区间上界"), _cmp_key(high, "区间上界"))
        except ExpressionEvaluationException:
            raise
        if low_closed:
            if above_low < 0:
                return False
        elif above_low <= 0:
            return False
        if high_closed:
            if above_high > 0:
                return False
        elif above_high >= 0:
            return False
        return True

    # -- 表达式层次 -------------------------------------------------------
    def additive(self) -> Any:
        val = self.mult()
        while True:
            op = self._peek_is_op("+", "-")
            if not op:
                return val
            self._next()
            rhs = self.mult()
            if op == "+":
                if isinstance(val, str) or isinstance(rhs, str):
                    if not (isinstance(val, str) and isinstance(rhs, str)):
                        raise ExpressionEvaluationException(
                            "字符串 + 仅支持字符串拼接"
                        )
                    val = val + rhs
                else:
                    val = _arith(val, rhs, "+", self.text)
            else:
                val = _arith(val, rhs, "-", self.text)

    def mult(self) -> Any:
        val = self.unary_expr()
        while True:
            op = self._peek_is_op("*", "/")
            if not op:
                return val
            self._next()
            val = _arith(val, self.unary_expr(), op, self.text)

    def unary_expr(self) -> Any:
        tok = self.cur
        if tok.kind == "OP" and tok.value == "-":
            self._next()
            return -_to_number(self.unary_expr(), self.text)
        return self.primary()

    def primary(self) -> Any:
        tok = self._next()
        if tok.kind == "NUM" or tok.kind == "STR":
            return tok.value
        if tok.kind == "IDENT":
            name = tok.value
            if name == "true":
                return True
            if name == "false":
                return False
            if name == "null":
                return None
            nxt = self.cur
            if nxt.kind == "OP" and nxt.value == "(":
                raise ExpressionEvaluationException(
                    f"FEEL 子集不支持函数调用 {name!r}(...): {self.text!r}"
                )
            # 变量引用（未定义 -> null，FEEL 缺变量语义）
            return self.vars.get(name)
        if tok.kind == "OP" and tok.value == "(":
            val = self.additive()
            self._expect_op(")")
            return val
        raise ExpressionEvaluationException(
            f"意外的记号 {tok.value!r}（位置 {tok.pos}）: {self.text!r}"
        )


def _apply_comparison(op: str, lhs: Any, rhs: Any) -> bool:
    if op == "=":
        return _values_equal(lhs, rhs)
    if op == "!=":
        return not _values_equal(lhs, rhs)
    return _cmp_bool(op, lhs, rhs)


def _cmp_bool(op: str, lhs: Any, rhs: Any) -> bool:
    c = _order_cmp(_cmp_key(lhs, f"比较 {op}"), _cmp_key(rhs, f"比较 {op}"))
    return {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[op]


def _arith(a: Any, b: Any, op: str, text: str) -> Any:
    if isinstance(a, str) or isinstance(b, str):
        raise ExpressionEvaluationException(
            f"算术 {op} 不支持字符串操作数: {text!r}"
        )
    if a is None or b is None:
        raise ExpressionEvaluationException(f"算术 {op} 操作数为 null: {text!r}")
    if isinstance(a, bool) or isinstance(b, bool):
        raise ExpressionEvaluationException(f"算术 {op} 操作数为布尔: {text!r}")
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                raise ExpressionEvaluationException(f"除零: {text!r}")
            r = a / b
            return r
    except TypeError as e:
        raise ExpressionEvaluationException(f"算术 {op} 类型错误: {text!r} ({e})") from e
    raise ExpressionEvaluationException(f"未知算术 {op}: {text!r}")


def _to_number(v: Any, text: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ExpressionEvaluationException(f"一元负号要求数值操作数: {text!r}")
    return v


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------
def eval_unary_test(text: Optional[str], input_value: Any) -> bool:
    """FEEL unaryTests 求值。text=None = 通配（恒命中）。"""
    if text is None:
        return True
    return _Parser(text, {}).eval_unary_test(input_value)


def eval_expression(text: str, variables: dict) -> Any:
    """FEEL expression 求值（输出单元格 / inputExpression）。"""
    return _Parser(text, variables or {}).eval()
