"""历史端点（M6-5）：实例 / 任务 / 活动 / 变量历史。

数据源：引擎内存中保留的流程实例（含已结束的——实例完成后仍留在
`_instances`，仅 state 置 COMPLETED）。启用 Store 时 ACT_HI_* 表同步写入，
但本端点统一走内存视图，保证两种模式下行为一致（文档化差异：历史查询
不做跨重启回溯，被 DELETE 删除的实例在内存视图中不再可见）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from camunda.api.deps import get_engine
from camunda.api.pagination import DEFAULT_MAX_RESULTS, paginate
from camunda.api.schemas import (
    activity_instance_dto,
    historic_process_instance_dto,
    historic_task_dto,
    to_variable_map,
)

router = APIRouter(tags=["history"])


@router.get("/history/process-instance", summary="历史流程实例")
def list_historic_process_instances(
    request: Request,
    processDefinitionKey: Optional[str] = Query(default=None),
    finished: Optional[bool] = Query(default=None, description="true=只看已结束"),
    businessKey: Optional[str] = Query(default=None),
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
    if finished is not None:
        instances = [pi for pi in instances if pi.is_completed == finished]
    items = [historic_process_instance_dto(pi) for pi in instances]
    return paginate(items, firstResult, maxResults)


@router.get("/history/process-instance/{instance_id}", summary="单个历史流程实例")
def get_historic_process_instance(
    request: Request, instance_id: str
) -> Dict[str, Any]:
    engine = get_engine(request)
    return historic_process_instance_dto(engine.get_process_instance(instance_id))


@router.get("/history/task", summary="历史任务（已归档 + 待办）")
def list_historic_tasks(
    request: Request,
    processInstanceId: Optional[str] = Query(default=None),
    finished: Optional[bool] = Query(default=None, description="true=只看已完成"),
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    engine = get_engine(request)
    out: List[Dict[str, Any]] = []
    instances = engine.list_process_instances()
    if processInstanceId is not None:
        instances = [pi for pi in instances if pi.id == processInstanceId]
    for pi in instances:
        if finished is not True:
            out.extend(historic_task_dto(t) for t in pi.completed_tasks)
        if finished is not False:
            # 待办任务（end_time 为空）也纳入历史视图，对齐 Camunda HistoricTaskInstance
            out.extend(
                historic_task_dto(t)
                for t in engine.create_task_query(process_instance_id=pi.id)
            )
    return paginate(out, firstResult, maxResults)


@router.get("/history/activity-instance", summary="历史活动实例")
def list_historic_activity_instances(
    request: Request,
    processInstanceId: Optional[str] = Query(default=None),
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    engine = get_engine(request)
    instances = engine.list_process_instances()
    if processInstanceId is not None:
        instances = [pi for pi in instances if pi.id == processInstanceId]
    items = [activity_instance_dto(ai) for pi in instances for ai in pi.activity_history]
    return paginate(items, firstResult, maxResults)


@router.get("/history/variable-instance", summary="历史变量（实例级快照）")
def list_historic_variable_instances(
    request: Request,
    processInstanceId: Optional[str] = Query(default=None),
    bare: bool = Query(default=False),
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    """变量历史：本项目为实例级快照语义（非每次变更追加版本，文档化差异）。"""
    engine = get_engine(request)
    instances = engine.list_process_instances()
    if processInstanceId is not None:
        instances = [pi for pi in instances if pi.id == processInstanceId]
    out: List[Dict[str, Any]] = []
    for pi in instances:
        for name, dto in to_variable_map(pi.variables, bare=bare).items():
            if bare:
                out.append(
                    {"processInstanceId": pi.id, "name": name, "value": dto}
                )
            else:
                out.append({"processInstanceId": pi.id, "name": name, **dto})
    return paginate(out, firstResult, maxResults)
