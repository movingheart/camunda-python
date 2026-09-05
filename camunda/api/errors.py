"""Camunda 异常层次 -> HTTP 状态码映射（M6-1）。

对齐 Camunda 7 REST 的错误响应体：
    {"type": "<异常类名>", "message": "<异常消息>"}

状态码决策（对齐 Camunda REST 常用语义，文档化差异见 docs/ARCHITECTURE.md）：
- NotFoundException            404  按 id/key 查不到对象
- DeploymentException          400  BPMN/DMN XML 语法错误或语义校验不通过
- InvalidRequestException      400  参数或调用不合法
- ProcessInstanceException     409  实例状态冲突（对已完成实例操作 / 定时启动流程手动启动）
- ExpressionEvaluationException 400 FEEL/UEL 求值失败
- CamundaException（兜底）     500  其余未分类引擎异常
"""

from __future__ import annotations

from typing import Any, Dict, Type

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from camunda.common.exceptions import (
    CamundaException,
    DeploymentException,
    ExpressionEvaluationException,
    InvalidRequestException,
    NotFoundException,
    ProcessInstanceException,
)

# 异常类 -> HTTP 状态码（子类查找按 MRO 顺序从最具体开始）
_STATUS_MAP: Dict[Type[BaseException], int] = {
    NotFoundException: 404,
    DeploymentException: 400,
    InvalidRequestException: 400,
    ProcessInstanceException: 409,
    ExpressionEvaluationException: 400,
    CamundaException: 500,
}


def status_for(exc: BaseException) -> int:
    """按异常类型解析 HTTP 状态码（未知引擎异常兜底 500）。"""
    for cls in type(exc).__mro__:
        if cls in _STATUS_MAP:
            return _STATUS_MAP[cls]
    return 500


def error_body(exc: BaseException) -> Dict[str, Any]:
    """构造 Camunda 风格错误响应体。"""
    return {"type": type(exc).__name__, "message": str(exc)}


def register_exception_handlers(app: FastAPI) -> None:
    """注册 CamundaException 全局处理器（FastAPI 按 MRO 派发最近的 handler）。"""

    @app.exception_handler(NotFoundException)
    async def _not_found(request: Request, exc: NotFoundException) -> JSONResponse:
        return JSONResponse(status_code=404, content=error_body(exc))

    @app.exception_handler(DeploymentException)
    async def _deployment(request: Request, exc: DeploymentException) -> JSONResponse:
        return JSONResponse(status_code=400, content=error_body(exc))

    @app.exception_handler(InvalidRequestException)
    async def _invalid(request: Request, exc: InvalidRequestException) -> JSONResponse:
        return JSONResponse(status_code=400, content=error_body(exc))

    @app.exception_handler(ProcessInstanceException)
    async def _conflict(
        request: Request, exc: ProcessInstanceException
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content=error_body(exc))

    @app.exception_handler(ExpressionEvaluationException)
    async def _expression(
        request: Request, exc: ExpressionEvaluationException
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content=error_body(exc))

    @app.exception_handler(CamundaException)
    async def _camunda(request: Request, exc: CamundaException) -> JSONResponse:
        return JSONResponse(status_code=500, content=error_body(exc))
