"""FastAPI 应用工厂（M6-1）。

用法：
    from camunda.api import create_app
    app = create_app()                      # 内存引擎，可直接 uvicorn 跑
    app = create_app(engine=my_engine)      # 复用已有引擎（含持久化/作业执行器）

端点总前缀默认 `/engine-rest`（对齐 Camunda 7 REST），可用 prefix 覆盖。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, FastAPI

from camunda import CAMUNDA_COMPAT_VERSION, __version__
from camunda.api.errors import register_exception_handlers
from camunda.api.routers import (
    decision,
    deployment,
    history,
    process_definition,
    process_instance,
    task,
)
from camunda.engine import ProcessEngine

DEFAULT_PREFIX = "/engine-rest"

_DESCRIPTION = f"""\
camunda-python 的 REST 兼容层（M6）。

对齐 Camunda {CAMUNDA_COMPAT_VERSION} engine-rest 的常用端点子集（文档化差异见
docs/ARCHITECTURE.md 的 M6 交付记录）。

- 变量入参兼容两种写法：包装形态 `{{"amount": {{"value": 1, "type": "Long"}}}}` 与裸值
  `{{"amount": 1}}`；出参默认包装形态，带 `?bare=true` 退化为裸值 map。
- 错误响应体统一为 `{{"type": "<异常类名>", "message": "<异常消息>"}}`。
"""


def create_app(
    engine: Optional[ProcessEngine] = None,
    prefix: str = DEFAULT_PREFIX,
    title: str = "camunda-python REST API",
) -> FastAPI:
    """构造 REST 应用。

    engine 为 None 时内部新建一个内存引擎（ProcessEngine()），适合 demo/测试；
    生产用法传入带 Store 的引擎实例即可（路由只依赖引擎门面方法）。
    """
    app = FastAPI(title=title, version=__version__, description=_DESCRIPTION)
    # 路由通过 request.app.state.engine 取引擎（见 camunda/api/deps.py）
    app.state.engine = engine if engine is not None else ProcessEngine()
    register_exception_handlers(app)

    api = APIRouter(prefix=prefix)
    api.include_router(deployment.router)
    api.include_router(process_definition.router)
    api.include_router(process_instance.router)
    api.include_router(task.router)
    api.include_router(history.router)
    api.include_router(decision.router)
    app.include_router(api)

    @app.get("/", tags=["meta"], summary="服务元信息")
    def index() -> dict:
        return {
            "name": "camunda-python",
            "version": __version__,
            "camundaCompat": CAMUNDA_COMPAT_VERSION,
            "engineRestPrefix": prefix,
        }

    @app.get("/health", tags=["meta"], summary="健康检查")
    def health() -> dict:
        return {"status": "UP"}

    return app
