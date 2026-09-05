"""REST 层依赖注入（M6-1）。

引擎实例挂在 app.state.engine（进程内单例），路由通过本模块取用，避免 routers
与 app 互相 import 造成循环依赖。
"""

from __future__ import annotations

from fastapi import Request

from camunda.engine import ProcessEngine


def get_engine(request: Request) -> ProcessEngine:
    """取应用绑定的引擎实例（create_app 时挂到 app.state.engine）。"""
    engine: ProcessEngine = request.app.state.engine
    return engine
