"""ISO-8601 时长 / 周期与 cron 触发时间计算（M3）。

BPMN timerEventDefinition 取值对齐 Camunda 7 语义：
- timeDuration  : ISO-8601 时长（如 PT30S / PT1H / P1D）→ 相对 duedate
- timeDate      : 绝对时间点（ISO-8601，可带 Z/时区偏移）→ 一次性 duedate
- timeCycle     : cron 表达式（quartz 风格，croniter 求值）或 ISO-8601 重复
                  周期 R[n]/PT..（如 R3/PT10S = 每 10 秒一次共 3 次）
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# 引擎统一时间格式（与 clock.now() 一致；定长可字典序比较）
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"

# ISO-8601 时长：P[nW] | P[nD][T[nH][nM][n[.f]S]]
_ISO_DURATION_RE = re.compile(
    r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)
# ISO-8601 重复周期：R[n]/<时长>（带起点形式 R/<date>/<dur> 暂不支持）
_ISO_REPEAT_RE = re.compile(r"^R(\d*)/(.+)$")


def format_iso(dt: datetime) -> str:
    """datetime -> 本地定长 ISO 字符串。"""
    return dt.strftime(ISO_FORMAT)


def parse_iso(text: str) -> datetime:
    """定长 ISO 字符串 -> datetime。"""
    return datetime.strptime(text, ISO_FORMAT)


def parse_trigger_date(text: str) -> str:
    """timeDate 文本 -> 本地定长 ISO 字符串（对齐引擎时钟时区）。"""
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # 退化为引擎定长格式直解（无时区本地时间）
        return format_iso(datetime.strptime(raw, ISO_FORMAT))
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # 转本地时区
    return format_iso(dt)


def parse_iso_duration(text: str) -> float:
    """ISO-8601 时长文本 -> 秒（浮点，支持小数秒）。

    例：PT30S -> 30；PT1H30M -> 5400；P1D -> 86400；P1W -> 604800。
    非法输入抛 ValueError（部署期即暴露配置错误）。
    """
    t = text.strip()
    sign = -1.0 if t.startswith("-") else 1.0
    t = t.lstrip("+-")
    m = _ISO_DURATION_RE.fullmatch(t)
    if not m or not any(m.groups()):
        raise ValueError(f"非法 ISO-8601 时长: {text!r}")
    weeks, days, hours, minutes, seconds = m.groups()
    total = (
        (int(weeks) if weeks else 0) * 604800
        + (int(days) if days else 0) * 86400
        + (int(hours) if hours else 0) * 3600
        + (int(minutes) if minutes else 0) * 60
        + (float(seconds) if seconds else 0.0)
    )
    return sign * total


def parse_iso_repeat(text: str) -> Optional[Dict[str, Any]]:
    """ISO-8601 重复周期 R[n]/PT.. -> repeat 字典；非该格式返回 None。

    repeat = {"kind": "interval", "seconds": float, "count": int | None}
    count=None 表示无限重复。
    """
    m = _ISO_REPEAT_RE.fullmatch(text.strip())
    if not m:
        return None
    count = int(m.group(1)) if m.group(1) else None
    seconds = parse_iso_duration(m.group(2))
    return {"kind": "interval", "seconds": seconds, "count": count}


def next_trigger(repeat: Dict[str, Any], after: datetime) -> datetime:
    """计算 timerCycle 在 after 之后的下一触发时刻。

    repeat:
    - {"kind": "interval", "seconds": s, "count": n|None}  间隔重复
    - {"kind": "cron", "expr": "..."}                      quartz/cron 表达式
    """
    if repeat["kind"] == "interval":
        return after + timedelta(seconds=repeat["seconds"])
    if repeat["kind"] == "cron":
        try:
            from croniter import croniter
        except ImportError as e:  # 依赖缺失时给出可操作提示
            raise RuntimeError(
                "timerCycle 使用 cron 表达式需要安装依赖 croniter"
            ) from e
        return croniter(repeat["expr"], after).get_next(datetime)
    raise ValueError(f"不支持的周期类型: {repeat!r}")
