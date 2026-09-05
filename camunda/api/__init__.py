"""api 包：REST 兼容层（M6 里程碑交付：FastAPI 对齐 Camunda engine-rest 常用端点）。

用法：
    from camunda.api import create_app
    app = create_app()                 # 内存引擎
    app = create_app(engine=engine)    # 复用既有引擎（含 Store / JobExecutor）

启动：
    uvicorn camunda.api.app:create_app --factory --port 8080
"""

from camunda.api.app import DEFAULT_PREFIX, create_app

__all__ = ["create_app", "DEFAULT_PREFIX"]
