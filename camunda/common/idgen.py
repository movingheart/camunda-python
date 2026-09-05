"""ID 生成器：对齐 Camunda 实体主键语义（UUID 版本）。"""

from __future__ import annotations

import uuid
from typing import Callable

# 可注入的 ID 生成函数（测试可替换为序列号以便断言）。
_generator: Callable[[], str] = lambda: uuid.uuid4().hex


def set_id_generator(fn: Callable[[], str]) -> None:
    """替换全局 ID 生成策略（主要用于测试确定性）。"""
    global _generator
    _generator = fn


class IdGenerator:
    """每次调用生成一个新的实体 ID。"""

    @staticmethod
    def next_id() -> str:
        return _generator()
