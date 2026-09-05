"""M6 REST 层单测：端点覆盖 + 异常映射（TestClient，无需起服务）。

覆盖：
- meta（/、/health）
- deployment（JSON 便捷通道 / multipart 多文件 / 列表 / BPMN-DMN 自动分派）
- process-definition（列表 / 按 key / XML）
- process-instance（启动 / 列表过滤 / 变量包装与裸值 / 设变量 / 删除）
- task（列表过滤 / 认领 / 取消 / 指派 / 完成 / 变量）
- history（实例 / 任务 / 活动 / 变量）
- decision-definition（列表 / 按 key / 求值）
- 异常映射（404 / 400 / 409 与 Camunda 风格错误体）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from camunda.api import create_app  # noqa: E402
from camunda.engine import ProcessEngine  # noqa: E402

EXAMPLES = ROOT / "examples"
PREFIX = "/engine-rest"


@pytest.fixture()
def client() -> TestClient:
    """每个用例一个全新内存引擎 + REST 应用（用例间互不干扰）。"""
    engine = ProcessEngine()
    engine.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    return TestClient(create_app(engine=engine))


def deploy_loan(client: TestClient) -> None:
    client.post(
        f"{PREFIX}/deployment/create/xml",
        json={"xml": (EXAMPLES / "loan-approval.bpmn").read_text(encoding="utf-8")},
    )


def start_loan(client: TestClient, **variables: Any) -> Dict[str, Any]:
    return client.post(
        f"{PREFIX}/process-instance",
        json={"definitionKey": "loan-approval", "variables": variables or None},
    ).json()


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
def test_meta_endpoints(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["engineRestPrefix"] == PREFIX and body["camundaCompat"] == "7.23.0"
    assert client.get("/health").json() == {"status": "UP"}


# ---------------------------------------------------------------------------
# deployment
# ---------------------------------------------------------------------------
def test_deploy_json_channel_and_list(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/deployment/create/xml",
        json={"xml": (EXAMPLES / "loan-approval.bpmn").read_text(encoding="utf-8")},
    )
    assert r.status_code == 200
    body = r.json()
    assert list(body["deployedProcessDefinitions"]) == ["loan-approval:1"]
    assert body["deployedProcessDefinitions"]["loan-approval:1"]["version"] == 1
    assert body["deployedDecisionDefinitions"] == {}

    deps = client.get(f"{PREFIX}/deployment").json()
    assert len(deps) == 1 and deps[0]["process_keys"] == ["loan-approval"]


def test_deploy_multipart_bpmn_and_dmn(client: TestClient) -> None:
    """multipart 一次部署 BPMN + DMN，按根元素子元素自动分派。"""
    bpmn = (EXAMPLES / "loan-approval.bpmn").read_text(encoding="utf-8")
    dmn = (EXAMPLES / "loan-grading.dmn").read_text(encoding="utf-8")
    r = client.post(
        f"{PREFIX}/deployment/create",
        files=[
            ("data", ("loan-approval.bpmn", bpmn, "text/xml")),
            ("data", ("loan-grading.dmn", dmn, "text/xml")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert "loan-approval:1" in body["deployedProcessDefinitions"]
    assert "loan-grading:1" in body["deployedDecisionDefinitions"]


def test_deploy_multipart_without_files_is_400(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/deployment/create")
    assert r.status_code == 400
    assert r.json()["type"] == "InvalidRequestException"


def test_deploy_invalid_xml_is_400(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/deployment/create/xml", json={"xml": "<not-xml"})
    assert r.status_code == 400
    assert r.json()["type"] == "DeploymentException"


def test_deploy_unclassifiable_xml_is_400(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/deployment/create/xml",
        json={"xml": "<definitions><other/></definitions>"},
    )
    assert r.status_code == 400 and r.json()["type"] == "DeploymentException"


# ---------------------------------------------------------------------------
# process-definition
# ---------------------------------------------------------------------------
def test_process_definition_list_get_xml(client: TestClient) -> None:
    deploy_loan(client)
    defs = client.get(f"{PREFIX}/process-definition").json()
    assert defs == [
        {"id": "loan-approval:1", "key": "loan-approval", "name": "贷款审批流程", "version": 1}
    ]
    one = client.get(f"{PREFIX}/process-definition/key/loan-approval").json()
    assert one["key"] == "loan-approval"
    xml = client.get(f"{PREFIX}/process-definition/key/loan-approval/xml").json()
    assert "<bpmn:process" in xml["bpmn20Xml"]


def test_process_definition_not_found(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/process-definition/key/nope")
    assert r.status_code == 404 and r.json()["type"] == "NotFoundException"


# ---------------------------------------------------------------------------
# process-instance
# ---------------------------------------------------------------------------
def test_start_instance_and_variable_forms(client: TestClient) -> None:
    deploy_loan(client)
    # 入参兼容：裸值 + 包装形态混用
    r = client.post(
        f"{PREFIX}/process-instance",
        json={
            "definitionKey": "loan-approval",
            "businessKey": "LOAN-1",
            "variables": {"applicant": "张三", "amount": {"value": 20000, "type": "Long"}},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["businessKey"] == "LOAN-1" and body["ended"] is False
    # 出参默认包装形态
    assert body["variables"]["amount"] == {"value": 20000, "type": "Long"}
    assert body["variables"]["applicant"]["value"] == "张三"
    # delegate 写入的变量同样可见
    assert body["variables"]["credit_ok"]["value"] is True

    # withVariablesInReturn / bare=true -> 裸值 map
    pi_id = body["id"]
    bare = client.get(f"{PREFIX}/process-instance/{pi_id}", params={"bare": "true"}).json()
    assert bare["variables"]["amount"] == 20000


def test_start_instance_missing_definition_key_is_400(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/process-instance", json={"variables": {}})
    assert r.status_code == 400 and r.json()["type"] == "InvalidRequestException"


def test_list_instances_filters(client: TestClient) -> None:
    deploy_loan(client)
    start_loan(client, amount=20000)
    start_loan(client, amount=1000)

    all_pi = client.get(f"{PREFIX}/process-instance").json()
    assert len(all_pi) == 2
    by_key = client.get(
        f"{PREFIX}/process-instance", params={"processDefinitionKey": "loan-approval"}
    ).json()
    assert len(by_key) == 2
    # amount=1000 走小额自动通过路径，实例直接结束
    finished = client.get(f"{PREFIX}/process-instance", params={"active": "false"}).json()
    assert len(finished) == 1
    running = client.get(f"{PREFIX}/process-instance", params={"active": "true"}).json()
    assert len(running) == 1


def test_get_set_variables(client: TestClient) -> None:
    deploy_loan(client)
    pi = start_loan(client, amount=20000)
    pi_id = pi["id"]

    # 裸值设置
    r = client.put(f"{PREFIX}/process-instance/{pi_id}/variables/score", json=95)
    assert r.status_code == 200 and r.json()["value"] == 95
    # 包装形态设置
    client.put(
        f"{PREFIX}/process-instance/{pi_id}/variables/level", json={"value": "A", "type": "String"}
    )
    variables = client.get(
        f"{PREFIX}/process-instance/{pi_id}/variables", params={"bare": "true"}
    ).json()
    assert variables["score"] == 95 and variables["level"] == "A"


def test_delete_instance(client: TestClient) -> None:
    deploy_loan(client)
    pi_id = start_loan(client, amount=20000)["id"]
    assert len(client.get(f"{PREFIX}/task").json()) == 1

    r = client.delete(f"{PREFIX}/process-instance/{pi_id}", params={"reason": "手工撤销"})
    assert r.status_code == 200 and r.json()["deleted"] is True
    # 运行时态清空：任务与实例均不可见
    assert client.get(f"{PREFIX}/task").json() == []
    assert client.get(f"{PREFIX}/process-instance/{pi_id}").status_code == 404


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------
def test_task_claim_unclaim_assignee(client: TestClient) -> None:
    deploy_loan(client)
    start_loan(client, amount=20000)
    task = client.get(f"{PREFIX}/task").json()[0]
    tid = task["id"]
    assert task["assignee"] is None

    # 未认领过滤
    assert len(client.get(f"{PREFIX}/task", params={"unassigned": "true"}).json()) == 1

    r = client.post(f"{PREFIX}/task/{tid}/claim", json={"userId": "lisi"})
    assert r.status_code == 200 and r.json()["assignee"] == "lisi"
    assert len(client.get(f"{PREFIX}/task", params={"assignee": "lisi"}).json()) == 1

    # 他人重复认领 -> 400
    r = client.post(f"{PREFIX}/task/{tid}/claim", json={"userId": "wangwu"})
    assert r.status_code == 400 and r.json()["type"] == "InvalidRequestException"

    # 直接指派不做校验
    r = client.post(f"{PREFIX}/task/{tid}/assignee", json={"userId": "zhaoliu"})
    assert r.json()["assignee"] == "zhaoliu"

    r = client.post(f"{PREFIX}/task/{tid}/unclaim")
    assert r.json()["assignee"] is None

    # claim 缺 userId -> 400
    assert client.post(f"{PREFIX}/task/{tid}/claim", json={}).status_code == 400


def test_task_complete_with_variables(client: TestClient) -> None:
    deploy_loan(client)
    pi_id = start_loan(client, amount=20000)["id"]
    tid = client.get(f"{PREFIX}/task").json()[0]["id"]

    r = client.post(f"{PREFIX}/task/{tid}/complete", json={"variables": {"approved": True}})
    assert r.status_code == 200 and r.json()["completed"] is True

    pi = client.get(f"{PREFIX}/process-instance/{pi_id}", params={"bare": "true"}).json()
    assert pi["ended"] is True and pi["variables"]["approved"] is True


def test_task_variables_are_instance_scoped(client: TestClient) -> None:
    """任务变量 = 实例变量（本项目变量实例级，文档化差异）。"""
    deploy_loan(client)
    start_loan(client, amount=20000)
    tid = client.get(f"{PREFIX}/task").json()[0]["id"]
    variables = client.get(f"{PREFIX}/task/{tid}/variables", params={"bare": "true"}).json()
    assert variables["amount"] == 20000


def test_complete_unknown_task_is_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/task/nope/complete", json={})
    assert r.status_code == 404 and r.json()["type"] == "NotFoundException"


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------
def test_history_endpoints(client: TestClient) -> None:
    deploy_loan(client)
    pi_id = start_loan(client, amount=20000)["id"]
    tid = client.get(f"{PREFIX}/task").json()[0]["id"]
    client.post(f"{PREFIX}/task/{tid}/claim", json={"userId": "lisi"})
    client.post(f"{PREFIX}/task/{tid}/complete", json={"variables": {"approved": True}})

    instances = client.get(
        f"{PREFIX}/history/process-instance", params={"finished": "true"}
    ).json()
    assert [i["id"] for i in instances] == [pi_id]
    assert instances[0]["endTime"] is not None

    one = client.get(f"{PREFIX}/history/process-instance/{pi_id}").json()
    assert one["processDefinitionKey"] == "loan-approval"

    tasks = client.get(
        f"{PREFIX}/history/task", params={"processInstanceId": pi_id}
    ).json()
    assert len(tasks) == 1 and tasks[0]["assignee"] == "lisi"
    assert tasks[0]["endTime"] is not None

    acts = client.get(
        f"{PREFIX}/history/activity-instance", params={"processInstanceId": pi_id}
    ).json()
    assert [a["activityId"] for a in acts] == [
        "start",
        "check-credit",
        "amount-gateway",
        "manual-review",
        "decision-gateway",
        "end-approved",
    ]

    varis = client.get(
        f"{PREFIX}/history/variable-instance",
        params={"processInstanceId": pi_id, "bare": "true"},
    ).json()
    names = {v["name"] for v in varis}
    assert {"amount", "approved", "credit_ok"} <= names


# ---------------------------------------------------------------------------
# decision-definition
# ---------------------------------------------------------------------------
def test_decision_endpoints(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/deployment/create/xml",
        json={"xml": (EXAMPLES / "loan-grading.dmn").read_text(encoding="utf-8")},
    )
    assert r.status_code == 200

    defs = client.get(f"{PREFIX}/decision-definition").json()
    assert {d["key"] for d in defs} == {"loan-grading", "rate-discount"}

    one = client.get(f"{PREFIX}/decision-definition/key/loan-grading").json()
    assert one["version"] == 1 and one["name"] == "贷款等级评定"

    # UNIQUE 单行命中
    r = client.post(
        f"{PREFIX}/decision-definition/key/loan-grading/evaluate",
        json={"variables": {"amount": 9000, "credit_score": 750}},
    )
    assert r.json()["result"] == "B"

    # COLLECT + SUM 多行命中聚合
    r = client.post(
        f"{PREFIX}/decision-definition/key/rate-discount/evaluate",
        json={"variables": {"grade": "A"}},
    )
    assert r.json()["result"] == 0.8


def test_evaluate_unknown_decision_is_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/decision-definition/key/nope/evaluate", json={})
    assert r.status_code == 404 and r.json()["type"] == "NotFoundException"
