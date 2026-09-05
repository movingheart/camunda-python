"""流程定义端点（M6-2）：GET /process-definition。"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query, Request

from camunda.api.deps import get_engine
from camunda.api.pagination import DEFAULT_MAX_RESULTS, paginate
from camunda.api.schemas import process_definition_dto

router = APIRouter(tags=["process-definition"])


@router.get("/process-definition", summary="流程定义列表")
def list_process_definitions(
    request: Request,
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    engine = get_engine(request)
    items = [
        process_definition_dto(d["key"], d["name"], d["version"])
        for d in engine.list_process_definitions()
    ]
    return paginate(items, firstResult, maxResults)


@router.get("/process-definition/key/{key}", summary="按 key 取最新版本的流程定义")
def get_process_definition_by_key(request: Request, key: str) -> Dict[str, Any]:
    engine = get_engine(request)
    proc = engine.get_process_definition(key)
    return process_definition_dto(key, proc.name, engine.get_definition_version(key))


@router.get("/process-definition/key/{key}/xml", summary="取流程定义 XML")
def get_process_definition_xml(request: Request, key: str) -> Dict[str, Any]:
    engine = get_engine(request)
    version = engine.get_definition_version(key)
    return {
        "id": f"{key}:{version}",
        "key": key,
        "version": version,
        # 部署时未带 source_xml（如直接 deploy(BpmnModel) 构造）则为 None
        "bpmn20Xml": engine.get_process_definition_xml(key),
    }
