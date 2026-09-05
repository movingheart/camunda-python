"""流程变量体系（M1 简化版）。

对齐 Camunda 变量语义的 Python 映射：
- Camunda 变量的 Java 类型名 -> Python 类型判断，序列化策略 JSON
- ObjectValue（Java 序列化对象）在 Python 侧无等价物：M1 用 JSON 可序列化对象表示，
  文档化差异（M2 持久化时落 TEXT/JSON 列）
"""

from __future__ import annotations

from typing import Any, Dict

# Java 类型 -> 判定函数（按需扩展）
_JAVA_TYPE_OF = [
    ("String", lambda v: isinstance(v, str)),
    ("Integer", lambda v: isinstance(v, int) and not isinstance(v, bool)),
    ("Boolean", lambda v: isinstance(v, bool)),
    ("Double", lambda v: isinstance(v, float)),
]


def java_type_name(value: Any) -> str:
    """推断变量值的 Java 类型名（Camunda REST API 的 type 字段）。"""
    for name, check in _JAVA_TYPE_OF:
        if check(value):
            return name
    return "Object"  # dict/list/None 等统一按对象处理


def to_typed_dict(variables: Dict[str, Any]) -> Dict[str, dict]:
    """把 {key: value} 转成 Camunda REST 风格 {key: {type, value}}（供 API 层/调试用）。"""
    return {
        k: {"type": java_type_name(v), "value": v}
        for k, v in variables.items()
    }
