"""DMN 决策端点（M6-6）：定义查询 + 决策求值。

DMN 部署走 /deployment/create（含 decision 子元素的 XML 自动分派到 deploy_dmn）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from camunda.api.deps import get_engine
from camunda.api.pagination import DEFAULT_MAX_RESULTS, paginate
from camunda.api.schemas import EvaluateDecisionDto, decision_definition_dto, from_variable_map

router = APIRouter(tags=["decision-definition"])


@router.get("/decision-definition", summary="决策定义列表")
def list_decision_definitions(
    request: Request,
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    engine = get_engine(request)
    items = [
        decision_definition_dto(d["key"], d["version"], d["name"])
        for d in engine.list_decision_definitions()
    ]
    return paginate(items, firstResult, maxResults)


@router.get("/decision-definition/key/{key}", summary="按 key 取最新版本的决策定义")
def get_decision_definition_by_key(request: Request, key: str) -> Dict[str, Any]:
    engine = get_engine(request)
    dec = engine.get_decision_definition(key)
    return decision_definition_dto(key, engine.get_decision_version(key), dec.name)


@router.post(
    "/decision-definition/key/{key}/evaluate", summary="求值决策表（返回原始结果）"
)
def evaluate_decision(
    request: Request, key: str, body: Optional[EvaluateDecisionDto] = None
) -> Dict[str, Any]:
    """求值决策表。

    返回 `result` 为引擎原始结果形态（单输出列 -> 标量；多输出列 -> dict；
    RULE ORDER/COLLECT -> 列表；无命中 -> None / []），不做 Camunda 的
    DmnDecisionResultEntries 包装（文档化差异）。
    """
    engine = get_engine(request)
    variables = from_variable_map(body.variables) if body is not None else {}
    result = engine.evaluate_decision(key, variables)
    return {"key": key, "variables": variables, "result": result}
