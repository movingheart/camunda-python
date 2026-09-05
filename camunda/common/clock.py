"""可注入时钟：引擎与 JobExecutor 统一取当前时间。

M2 用 time.strftime 散落各处取字符串时间；M3 起统一走本模块：
- 输出为本地时区定长 ISO 字符串 "%Y-%m-%dT%H:%M:%S"（同长可字典序比较 due）
- 测试可用 set_clock 冻结 / 拨快时间，无需真 sleep 即可验证定时器到期语义
"""

from __future__ import annotations

import time
from typing import Callable

_DEFAULT = lambda: time.strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
_fn: Callable[[], str] = _DEFAULT


def now() -> str:
    return _fn()


def set_clock(fn: Callable[[], str]) -> None:
    """替换当前时间来源（测试注入 fake clock）。"""
    global _fn
    _fn = fn


def reset_clock() -> None:
    """恢复真实系统时钟。"""
    global _fn
    _fn = _DEFAULT
