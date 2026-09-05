"""REST 分页（M8）。

对齐 Camunda 7 REST 分页约定：
- 查询参数：`firstResult`（0 基偏移，默认 0）+ `maxResults`（每页上限，默认 200）
- 响应：仍是裸数组（**不**回包 count/total），调用方通过「结果数 < maxResults」判定到达末页
- 负值 / 越界自动 clamp 到合法区间（Camunda 对非法 firstResult 直接抛 400，
  本项目为易用性选 clamp，文档化差异——见 docs/ARCHITECTURE.md）

用法：

    @router.get("/foo")
    def list_foo(
        request: Request,
        firstResult: int = Query(default=0, ge=0),
        maxResults: int = Query(default=DEFAULT_MAX_RESULTS, ge=1, le=MAX_RESULTS_LIMIT),
    ):
        items = ...
        return paginate(items, firstResult, maxResults)

设计动机：本项目 M6 列表端点全部 `return list`——分页前移到这里实现
一遍，所有路由都用同一份语义与默认值，避免每个端点各写一份。
"""

from __future__ import annotations

from typing import Any, Dict, List, TypeVar

# Camunda 默认对 maxResults 不设硬上限；本项目为防止脚本误用拉空内存，
# 取一个 Camunda 文档中常见的"列表默认上限 200"作为软上限。
DEFAULT_MAX_RESULTS = 200
MAX_RESULTS_LIMIT = 1000  # 单次请求硬上限，超过会被 clamp 到这里

T = TypeVar("T")


def normalize_pagination(
    first_result: int, max_results: int
) -> Dict[str, int]:
    """把入参 clamp 到合法区间，返回 {firstResult, maxResults} dict。

    - firstResult < 0 -> 0
    - maxResults < 1 -> DEFAULT_MAX_RESULTS（前端误传 0/负数）
    - maxResults > MAX_RESULTS_LIMIT -> MAX_RESULTS_LIMIT
    """
    fr = max(0, int(first_result))
    mr = int(max_results) if max_results is not None else DEFAULT_MAX_RESULTS
    if mr < 1:
        mr = DEFAULT_MAX_RESULTS
    if mr > MAX_RESULTS_LIMIT:
        mr = MAX_RESULTS_LIMIT
    return {"firstResult": fr, "maxResults": mr}


def paginate(
    items: List[T], first_result: int, max_results: int
) -> List[T]:
    """对列表做切片（闭区间语义）。

    返回值仍是裸列表，便于 FastAPI 直接 JSON 序列化。
    """
    norm = normalize_pagination(first_result, max_results)
    fr, mr = norm["firstResult"], norm["maxResults"]
    # 起点超过尾：返回空数组（Camunda 行为相同）
    if fr >= len(items):
        return []
    return items[fr : fr + mr]


__all__ = [
    "DEFAULT_MAX_RESULTS",
    "MAX_RESULTS_LIMIT",
    "normalize_pagination",
    "paginate",
]
