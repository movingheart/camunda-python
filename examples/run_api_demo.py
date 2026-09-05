"""M6 REST API 演示（真实起服务 + 真实 HTTP 调用）。

用法：
    python examples/run_api_demo.py

演示内容（端到端走 HTTP，不直接调引擎）：
    1. 部署：JSON 便捷通道部署贷款审批流程；multipart 一次部署 BPMN + DMN
    2. 启动：按 key 启动实例（变量兼容裸值与包装形态）
    3. 任务：列表 -> 认领 -> 带变量完成 -> 实例结束
    4. 历史：实例 / 任务 / 活动 / 变量四类历史查询
    5. 决策：DMN 决策表求值（UNIQUE 单行命中 + COLLECT SUM 聚合）
    6. 异常：404 / 400 的 Camunda 风格错误体

说明：脚本内部起 uvicorn（随机端口，后台线程），末尾断言全部通过后自动关停。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camunda.api import create_app  # noqa: E402
from camunda.engine import ProcessEngine  # noqa: E402

EXAMPLES = ROOT / "examples"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server(uvicorn.Server):
    """可后台启停的 uvicorn（install_signal_handlers=False 避免抢主进程信号）。"""

    def install_signal_handlers(self) -> None:  # noqa: D102
        pass


def _start_server(port: int) -> tuple[_Server, threading.Thread]:
    engine = ProcessEngine()
    engine.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    config = uvicorn.Config(
        create_app(engine=engine), host="127.0.0.1", port=port, log_level="warning"
    )
    server = _Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            return server, thread
        time.sleep(0.05)
    raise RuntimeError("uvicorn 启动超时")


def _banner(text: str) -> None:
    print("=" * 66)
    print(text)
    print("=" * 66)


def demo_deploy(base: str) -> None:
    """演示 1：部署（JSON 通道 + multipart 混合部署）。"""
    _banner("演示 1：部署（JSON 便捷通道 + multipart 一次部署 BPMN/DMN）")
    r = httpx.post(
        f"{base}/deployment/create/xml",
        json={"xml": (EXAMPLES / "loan-approval.bpmn").read_text(encoding="utf-8")},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    print(f"    JSON 部署 -> {list(r.json()['deployedProcessDefinitions'])}")

    r = httpx.post(
        f"{base}/deployment/create",
        files=[
            ("data", ("loan-grading.dmn", (EXAMPLES / "loan-grading.dmn").read_text(encoding="utf-8"), "text/xml")),
            ("data", ("loan-grading-flow.bpmn", (EXAMPLES / "loan-grading-flow.bpmn").read_text(encoding="utf-8"), "text/xml")),
        ],
        timeout=10,
    )
    assert r.status_code == 200, r.text
    print(f"    multipart 部署 -> 决策 {list(r.json()['deployedDecisionDefinitions'])}")

    defs = httpx.get(f"{base}/process-definition", timeout=10).json()
    print(f"    流程定义列表 -> {[(d['key'], d['version']) for d in defs]}")
    print()


def demo_instance_and_task(base: str) -> str:
    """演示 2+3：启动实例 -> 任务认领 -> 带变量完成。"""
    _banner("演示 2：启动流程实例（变量兼容裸值与包装形态）")
    r = httpx.post(
        f"{base}/process-instance",
        json={
            "definitionKey": "loan-approval",
            "businessKey": "LOAN-1001",
            "variables": {"applicant": "张三", "amount": {"value": 20000, "type": "Long"}},
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    pi = r.json()
    print(f"    实例 {pi['id']} 已启动（businessKey={pi['businessKey']}）")
    print(f"    变量（包装形态）-> amount={pi['variables']['amount']}")

    bare = httpx.get(f"{base}/process-instance/{pi['id']}", params={"bare": "true"}, timeout=10).json()
    print(f"    ?bare=true 裸值 -> amount={bare['variables']['amount']}")

    _banner("演示 3：任务列表 -> 认领 -> 完成")
    tasks = httpx.get(f"{base}/task", timeout=10).json()
    tid = tasks[0]["id"]
    print(f"    待办任务 -> {tasks[0]['name']}（assignee={tasks[0]['assignee']}）")

    r = httpx.post(f"{base}/task/{tid}/claim", json={"userId": "lisi"}, timeout=10)
    assert r.json()["assignee"] == "lisi"
    print(f"    认领后 assignee={r.json()['assignee']}")

    r = httpx.post(f"{base}/task/{tid}/complete", json={"variables": {"approved": True}}, timeout=10)
    assert r.status_code == 200, r.text
    print("    完成任务（变量 approved=True）")

    ended = httpx.get(f"{base}/process-instance/{pi['id']}", timeout=10).json()
    assert ended["ended"] is True
    print(f"    实例已结束 ended={ended['ended']}")
    print()
    return pi["id"]


def demo_history(base: str, pi_id: str) -> None:
    """演示 4：四类历史查询。"""
    _banner("演示 4：历史查询（实例 / 任务 / 活动 / 变量）")
    r = httpx.get(f"{base}/history/process-instance/{pi_id}", timeout=10).json()
    print(f"    历史实例 -> {r['processDefinitionKey']} start={r['startTime']}")

    tasks = httpx.get(f"{base}/history/task", params={"processInstanceId": pi_id}, timeout=10).json()
    print(f"    历史任务 -> {[(t['name'], t['assignee']) for t in tasks]}")

    acts = httpx.get(
        f"{base}/history/activity-instance", params={"processInstanceId": pi_id}, timeout=10
    ).json()
    print(f"    活动轨迹 -> {' -> '.join(a['activityId'] for a in acts)}")

    varis = httpx.get(
        f"{base}/history/variable-instance",
        params={"processInstanceId": pi_id, "bare": "true"},
        timeout=10,
    ).json()
    vars_text = ", ".join(f"{v['name']}={v['value']}" for v in varis)
    print(f"    历史变量 -> {vars_text}")
    print()


def demo_decision(base: str) -> None:
    """演示 5：DMN 决策求值（BPMN 集成 + 独立求值）。"""
    _banner("演示 5：DMN 决策求值（businessRuleTask 集成 + 独立求值）")
    r = httpx.post(
        f"{base}/decision-definition/key/loan-grading/evaluate",
        json={"variables": {"amount": 20000, "credit_score": 500}},
        timeout=10,
    )
    assert r.json()["result"] == "C"
    print(f"    独立求值 20000/500 -> 等级 {r.json()['result']}")

    r = httpx.post(
        f"{base}/process-instance",
        json={"definitionKey": "loan-process", "variables": {"amount": 20000, "credit_score": 500}},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    pi = r.json()
    grade = httpx.get(f"{base}/process-instance/{pi['id']}/variables", params={"bare": "true"}, timeout=10).json()["grade"]
    assert grade == "C"
    print(f"    businessRuleTask 驱动 -> 实例变量 grade={grade}")

    # C 级进人工复核
    tasks = httpx.get(f"{base}/task", params={"processInstanceId": pi["id"]}, timeout=10).json()
    assert tasks and tasks[0]["taskDefinitionKey"] == "review"
    print(f"    grade=C -> 进入人工复核任务 {tasks[0]['name']}")
    print()


def demo_errors(base: str) -> None:
    """演示 6：异常映射与 Camunda 风格错误体。"""
    _banner("演示 6：异常映射（Camunda 风格错误体）")
    r = httpx.get(f"{base}/process-instance/not-exist", timeout=10)
    print(f"    查不存在的实例 -> {r.status_code} {r.json()}")

    r = httpx.post(f"{base}/deployment/create/xml", json={"xml": "<not-xml"}, timeout=10)
    print(f"    部署非法 XML    -> {r.status_code} {r.json()}")
    assert r.status_code == 400 and r.json()["type"] == "DeploymentException"
    print()


def demo_pagination(base: str) -> None:
    """演示 7：M8 分页（firstResult + maxResults）跨 9 个列表端点。"""
    _banner("演示 7：列表分页（firstResult + maxResults）")
    # 起 5 个实例，方便演示分页
    for _ in range(5):
        httpx.post(
            f"{base}/process-instance",
            json={"definitionKey": "loan-approval", "variables": {"amount": 5000}},
            timeout=10,
        )

    full = httpx.get(f"{base}/process-instance", timeout=10).json()
    page1 = httpx.get(
        f"{base}/process-instance", params={"firstResult": 0, "maxResults": 2}, timeout=10
    ).json()
    page2 = httpx.get(
        f"{base}/process-instance", params={"firstResult": 2, "maxResults": 2}, timeout=10
    ).json()
    last = httpx.get(
        f"{base}/process-instance", params={"firstResult": 4, "maxResults": 10}, timeout=10
    ).json()
    print(f"    全量 ({len(full)}) -> 取 firstResult=0/maxResults=2 -> {len(page1)} 条")
    print(f"    接着 firstResult=2/maxResults=2 -> {len(page2)} 条（与首页不重叠）")
    print(f"    末页 firstResult=4/maxResults=10 -> {len(last)} 条（< maxResults 即末页）")

    # 越界返回空数组
    over = httpx.get(
        f"{base}/process-instance", params={"firstResult": 999}, timeout=10
    ).json()
    print(f"    越界 firstResult=999 -> {over}")

    # maxResults 超 1000 自动 clamp
    clamped = httpx.get(
        f"{base}/process-instance", params={"maxResults": 99999}, timeout=10
    ).json()
    print(f"    maxResults=99999 -> 返回 {len(clamped)} 条（内部 clamp 到 1000）")

    # 非法值被 FastAPI 拒绝
    bad = httpx.get(
        f"{base}/process-instance", params={"firstResult": -1}, timeout=10
    )
    print(f"    firstResult=-1 -> {bad.status_code}（FastAPI ge=0 校验拒绝）")

    # 历史分页
    hist = httpx.get(
        f"{base}/history/process-instance",
        params={"firstResult": 0, "maxResults": 3},
        timeout=10,
    ).json()
    print(f"    history/process-instance 分页 -> {len(hist)} 条")
    print()


def main() -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}/engine-rest"
    server, _thread = _start_server(port)
    print(f"uvicorn 已启动：http://127.0.0.1:{port}（REST 前缀 /engine-rest，"
          f"交互式文档 /docs）\n")
    try:
        demo_deploy(base)
        pi_id = demo_instance_and_task(base)
        demo_history(base, pi_id)
        demo_decision(base)
        demo_errors(base)
        demo_pagination(base)
        _banner("全部演示通过 ✅")
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
