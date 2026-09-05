"""流程实例端点（M6-3）：启动 / 查询 / 变量 / 删除。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query, Request

from camunda.api.deps import get_engine
from camunda.api.pagination import DEFAULT_MAX_RESULTS, paginate
from camunda.api.schemas import (
    StartProcessInstanceDto,
    from_variable_map,
    process_instance_dto,
    to_variable_map,
)
from camunda.common.exceptions import InvalidRequestException

router = APIRouter(tags=["process-instance"])


@router.post("/process-instance", summary="按 key 启动流程实例")
def start_process_instance(
    request: Request, body: StartProcessInstanceDto
) -> Dict[str, Any]:
    engine = get_engine(request)
    if not body.definitionKey:
        raise InvalidRequestException(
            "启动流程须提供 definitionKey（Camunda 的 definitionId / message 启动 M6 不支持）"
        )
    pi = engine.start_process_instance_by_key(
        body.definitionKey,
        variables=from_variable_map(body.variables),
        business_key=body.businessKey,
    )
    return process_instance_dto(pi, bare=bool(body.withVariablesInReturn))


@router.get("/process-instance", summary="流程实例列表（含已结束）")
def list_process_instances(
    request: Request,
    processDefinitionKey: Optional[str] = Query(default=None),
    businessKey: Optional[str] = Query(default=None),
    active: Optional[bool] = Query(default=None, description="true=只看运行中"),
    bare: bool = Query(default=False, description="变量退化为裸值 map"),
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    engine = get_engine(request)
    instances = engine.list_process_instances()
    if processDefinitionKey is not None:
        instances = [
            pi for pi in instances if pi.process_definition_key == processDefinitionKey
        ]
    if businessKey is not None:
        instances = [pi for pi in instances if pi.business_key == businessKey]
    if active is not None:
        instances = [pi for pi in instances if (not pi.is_completed) == active]
    items = [process_instance_dto(pi, bare=bare) for pi in instances]
    return paginate(items, firstResult, maxResults)


@router.get("/process-instance/{instance_id}", summary="按 id 取流程实例")
def get_process_instance(
    request: Request, instance_id: str, bare: bool = Query(default=False)
) -> Dict[str, Any]:
    engine = get_engine(request)
    return process_instance_dto(engine.get_process_instance(instance_id), bare=bare)


@router.delete("/process-instance/{instance_id}", summary="删除流程实例（历史保留）")
def delete_process_instance(
    request: Request, instance_id: str, reason: Optional[str] = Query(default=None)
) -> Dict[str, Any]:
    """删除实例：清运行时态 + RU 行，HI_PROCINST 置 DELETED（对齐 Camunda 默认）。"""
    engine = get_engine(request)
    engine.delete_process_instance(instance_id, reason=reason)
    return {"deleted": True, "id": instance_id, "reason": reason}


@router.get("/process-instance/{instance_id}/variables", summary="实例变量列表")
def get_variables(
    request: Request, instance_id: str, bare: bool = Query(default=False)
) -> Dict[str, Any]:
    engine = get_engine(request)
    pi = engine.get_process_instance(instance_id)
    return to_variable_map(pi.variables, bare=bare)


@router.put(
    "/process-instance/{instance_id}/variables/{name}", summary="设置单个实例变量"
)
def put_variable(
    request: Request,
    instance_id: str,
    name: str,
    # Body() 显式标注：否则 Any + 默认值会被 FastAPI 当成非 body 参数（静默收不到）
    body: Any = Body(default=None),
) -> Dict[str, Any]:
    """设置变量。body 支持包装形态 {"value": x} 与裸值（如直接传 20000 / "abc"）。"""
    engine = get_engine(request)
    value = from_variable_map({"v": body})["v"] if isinstance(body, dict) else body
    engine.set_variable(instance_id, name, value)
    return {"name": name, "value": value, "processInstanceId": instance_id}
