"""M8 REST 分页单测：firstResult + maxResults 在 9 个列表端点上行为一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from camunda.api import create_app  # noqa: E402
from camunda.api.pagination import (  # noqa: E402
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_LIMIT,
    normalize_pagination,
    paginate,
)
from camunda.engine import ProcessEngine  # noqa: E402

EXAMPLES = ROOT / "examples"
PREFIX = "/engine-rest"


# ---------------------------------------------------------------------------
# 纯函数 helper
# ---------------------------------------------------------------------------
class TestNormalizePagination:
    """normalize_pagination / paginate 的纯函数行为。"""

    def test_defaults(self):
        n = normalize_pagination(0, DEFAULT_MAX_RESULTS)
        assert n == {"firstResult": 0, "maxResults": DEFAULT_MAX_RESULTS}

    def test_negative_firstResult_clamped(self):
        assert normalize_pagination(-5, 50)["firstResult"] == 0

    def test_zero_maxResults_falls_back(self):
        # 0 是非法的，会回退到默认
        assert normalize_pagination(0, 0)["maxResults"] == DEFAULT_MAX_RESULTS

    def test_negative_maxResults_falls_back(self):
        assert normalize_pagination(0, -3)["maxResults"] == DEFAULT_MAX_RESULTS

    def test_maxResults_capped(self):
        n = normalize_pagination(0, 99999)
        assert n["maxResults"] == MAX_RESULTS_LIMIT


class TestPaginate:
    """列表切片边界。"""

    def test_first_page(self):
        out = paginate([1, 2, 3, 4, 5], first_result=0, max_results=2)
        assert out == [1, 2]

    def test_middle_page(self):
        out = paginate([1, 2, 3, 4, 5], first_result=2, max_results=2)
        assert out == [3, 4]

    def test_last_partial_page_signals_end(self):
        """结果数 < maxResults 即末页（Camunda 风格）。"""
        out = paginate([1, 2, 3, 4, 5], first_result=4, max_results=10)
        assert out == [5]

    def test_offset_past_end_returns_empty(self):
        out = paginate([1, 2, 3], first_result=10, max_results=5)
        assert out == []

    def test_negative_offset_clamped_to_first_page(self):
        out = paginate([1, 2, 3, 4, 5], first_result=-1, max_results=3)
        assert out == [1, 2, 3]

    def test_maxResults_over_limit_clamped(self):
        """超限 maxResults 切到 MAX_RESULTS_LIMIT；列表够长时返回整个列表。"""
        big = list(range(50))
        out = paginate(big, first_result=0, max_results=99999)
        assert len(out) == len(big)  # 50 < 1000，未截断


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------
@pytest.fixture()
def client() -> TestClient:
    """引擎 + 一个 loan-approval 部署，足够覆盖所有列表端点。"""
    engine = ProcessEngine()
    engine.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    app = create_app(engine=engine)
    cli = TestClient(app)
    cli.post(
        f"{PREFIX}/deployment/create/xml",
        json={"xml": (EXAMPLES / "loan-approval.bpmn").read_text(encoding="utf-8")},
    )
    return cli


def _seed_instances(client: TestClient, n: int) -> None:
    for _ in range(n):
        client.post(
            f"{PREFIX}/process-instance",
            json={"definitionKey": "loan-approval", "variables": {}},
        )


class TestPaginationOnProcessInstance:
    """最常用的列表端点。"""

    def test_default_returns_all(self, client: TestClient):
        _seed_instances(client, 5)
        r = client.get(f"{PREFIX}/process-instance")
        assert r.status_code == 200
        assert len(r.json()) == 5

    def test_firstResult_and_maxResults(self, client: TestClient):
        _seed_instances(client, 5)
        page1 = client.get(
            f"{PREFIX}/process-instance?firstResult=0&maxResults=2"
        ).json()
        page2 = client.get(
            f"{PREFIX}/process-instance?firstResult=2&maxResults=2"
        ).json()
        assert len(page1) == 2 and len(page2) == 2
        # 不重叠
        assert {p["id"] for p in page1}.isdisjoint({p["id"] for p in page2})

    def test_maxResults_above_limit_clamped(self, client: TestClient):
        _seed_instances(client, 3)
        # 请求 99999，超过 MAX_RESULTS_LIMIT=1000；列表只有 3 条，不会截断
        r = client.get(f"{PREFIX}/process-instance?maxResults=99999")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_negative_firstResult_rejected_by_fastapi(self, client: TestClient):
        """Query(ge=0) 由 FastAPI 在进入路由前拒绝（422），不走 normalize 的 clamp。

        文档化差异：Camunda 对非法 firstResult 同样返回 400；本项目用 FastAPI 默认 422。
        """
        _seed_instances(client, 3)
        r = client.get(f"{PREFIX}/process-instance?firstResult=-5&maxResults=10")
        assert r.status_code == 422

    def test_offset_past_end_returns_empty(self, client: TestClient):
        _seed_instances(client, 3)
        r = client.get(f"{PREFIX}/process-instance?firstResult=999&maxResults=10")
        assert r.status_code == 200
        assert r.json() == []

    def test_pagination_with_filter(self, client: TestClient):
        """分页与过滤复合：先过滤再分页。"""
        _seed_instances(client, 4)
        r = client.get(
            f"{PREFIX}/process-instance"
            "?processDefinitionKey=loan-approval&firstResult=1&maxResults=2"
        )
        assert r.status_code == 200
        assert len(r.json()) == 2


class TestPaginationOnOtherLists:
    """9 个列表端点都要支持——逐个冒烟。"""

    def test_process_definition(self, client: TestClient):
        # loan-approval 已经部署一份
        r = client.get(f"{PREFIX}/process-definition?firstResult=0&maxResults=1")
        assert r.status_code == 200
        assert len(r.json()) == 1
        all_defs = client.get(f"{PREFIX}/process-definition").json()
        assert len(all_defs) == 1

    def test_task_list(self, client: TestClient):
        # 启动 1 个实例（loan-approval 入口 exclusive gateway 需要 amount 变量）
        client.post(
            f"{PREFIX}/process-instance",
            json={"definitionKey": "loan-approval", "variables": {"amount": 15000}},
        )
        r = client.get(f"{PREFIX}/task?maxResults=10")
        assert r.status_code == 200
        assert len(r.json()) >= 1

    def test_deployment(self, client: TestClient):
        # 已经部署了 1 次
        r = client.get(f"{PREFIX}/deployment?firstResult=0&maxResults=5")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_history_process_instance(self, client: TestClient):
        _seed_instances(client, 3)
        r = client.get(f"{PREFIX}/history/process-instance?maxResults=2")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_history_task(self, client: TestClient):
        client.post(
            f"{PREFIX}/process-instance",
            json={"definitionKey": "loan-approval", "variables": {"amount": 15000}},
        )
        r = client.get(f"{PREFIX}/history/task?firstResult=0&maxResults=100")
        assert r.status_code == 200
        assert len(r.json()) >= 1

    def test_history_activity(self, client: TestClient):
        _seed_instances(client, 2)
        r = client.get(f"{PREFIX}/history/activity-instance?maxResults=10")
        assert r.status_code == 200
        assert len(r.json()) >= 1

    def test_history_variable(self, client: TestClient):
        _seed_instances(client, 1)
        r = client.get(f"{PREFIX}/history/variable-instance?maxResults=10")
        assert r.status_code == 200
        # loan-approval 启动时无变量，但仍返回空列表（不报错）
        assert isinstance(r.json(), list)

    def test_decision_definition(self, client: TestClient):
        """M8 端点冒烟：决策定义列表支持分页（无部署时返回空）。"""
        r = client.get(f"{PREFIX}/decision-definition?maxResults=10")
        assert r.status_code == 200
        assert r.json() == []


class TestPaginationRobustness:
    """Camunda 风格参数校验边界。"""

    def test_firstResult_rejects_negative_via_fastapi(self, client: TestClient):
        """ge=0 校验：负值会被 FastAPI 拦下（422），不走 clamp。"""
        r = client.get(f"{PREFIX}/process-instance?firstResult=-1")
        assert r.status_code == 422

    def test_maxResults_rejects_zero_via_fastapi(self, client: TestClient):
        """ge=1 校验：0 会返回 422。"""
        r = client.get(f"{PREFIX}/process-instance?maxResults=0")
        assert r.status_code == 422
