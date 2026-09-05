"""任务端点（M6-4）：查询 / 认领 / 完成 / 变量。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from camunda.api.deps import get_engine
from camunda.api.pagination import DEFAULT_MAX_RESULTS, paginate
from camunda.api.schemas import (
    CompleteTaskDto,
    UserIdDto,
    from_variable_map,
    task_dto,
    to_variable_map,
)

router = APIRouter(tags=["task"])


@router.get("/task", summary="任务列表（活跃待办）")
def list_tasks(
    request: Request,
    processInstanceId: Optional[str] = Query(default=None),
    assignee: Optional[str] = Query(default=None),
    candidateUser: Optional[str] = Query(default=None),
    unassigned: Optional[bool] = Query(default=None, description="true=只看未认领"),
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    engine = get_engine(request)
    tasks = engine.create_task_query(process_instance_id=processInstanceId)
    if assignee is not None:
        tasks = [t for t in tasks if t.assignee == assignee]
    if candidateUser is not None:
        tasks = [t for t in tasks if candidateUser in t.candidate_users]
    if unassigned:
        tasks = [t for t in tasks if t.assignee is None]
    items = [task_dto(t) for t in tasks]
    return paginate(items, firstResult, maxResults)


@router.get("/task/{task_id}", summary="按 id 取任务")
def get_task(request: Request, task_id: str) -> Dict[str, Any]:
    engine = get_engine(request)
    return task_dto(engine.get_task(task_id))


@router.post("/task/{task_id}/claim", summary="认领任务")
def claim_task(request: Request, task_id: str, body: UserIdDto) -> Dict[str, Any]:
    engine = get_engine(request)
    if not body.userId:
        from camunda.common.exceptions import InvalidRequestException

        raise InvalidRequestException("claim 需要 userId")
    return task_dto(engine.claim_task(task_id, body.userId))


@router.post("/task/{task_id}/unclaim", summary="取消认领")
def unclaim_task(request: Request, task_id: str) -> Dict[str, Any]:
    engine = get_engine(request)
    return task_dto(engine.unclaim_task(task_id))


@router.post("/task/{task_id}/assignee", summary="直接指派（不做已认领校验）")
def set_assignee(request: Request, task_id: str, body: UserIdDto) -> Dict[str, Any]:
    engine = get_engine(request)
    return task_dto(engine.set_assignee(task_id, body.userId))


@router.post("/task/{task_id}/complete", summary="完成任务（合并变量后推进）")
def complete_task(
    request: Request, task_id: str, body: Optional[CompleteTaskDto] = None
) -> Dict[str, Any]:
    engine = get_engine(request)
    variables = from_variable_map(body.variables) if body is not None else {}
    engine.complete_task(task_id, variables=variables)
    return {"completed": True, "id": task_id}


@router.get("/task/{task_id}/variables", summary="任务可见变量（= 实例变量）")
def get_task_variables(
    request: Request, task_id: str, bare: bool = Query(default=False)
) -> Dict[str, Any]:
    """任务变量：本项目变量为实例级（文档化差异），故返回所属实例的变量全集。"""
    engine = get_engine(request)
    task = engine.get_task(task_id)
    pi = engine.get_process_instance(task.process_instance_id)
    return to_variable_map(pi.variables, bare=bare)
