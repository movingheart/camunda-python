"""M2 持久化测试：ACT 表落库 + 崩溃恢复 + 历史写入。

核心场景：
1. deploy 落库 -> 新引擎 from_database 仍能启动流程（定义恢复）
2. 实例停在 userTask 时"崩溃" -> 恢复 -> 变量/任务完好 -> complete -> COMPLETED
3. 并行 join 停等状态恢复（3 分支完成 2 支后重启，join 等待登记要重建）
4. HI 历史表写入（PROCINST/ACTINST/TASKINST/VARINST）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from camunda.engine import ProcessEngine
from camunda.persistence.entities import (
    HistActInstEntity,
    HistProcInstEntity,
    HistTaskInstEntity,
    HistVarInstEntity,
)
from camunda.persistence.store import Store
from camunda.parser import parse_bpmn_file

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# 三分支并行：fork 3 userTask -> join 汇聚
FORK3_XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  targetNamespace="http://example.camunda.python/fork3">
  <bpmn:process id="fork3" name="三分支并行" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:parallelGateway id="fork"/>
    <bpmn:userTask id="t1" name="任务一"/>
    <bpmn:userTask id="t2" name="任务二"/>
    <bpmn:userTask id="t3" name="任务三"/>
    <bpmn:parallelGateway id="join"/>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f0" sourceRef="start" targetRef="fork"/>
    <bpmn:sequenceFlow id="f1" sourceRef="fork" targetRef="t1"/>
    <bpmn:sequenceFlow id="f2" sourceRef="fork" targetRef="t2"/>
    <bpmn:sequenceFlow id="f3" sourceRef="fork" targetRef="t3"/>
    <bpmn:sequenceFlow id="g1" sourceRef="t1" targetRef="join"/>
    <bpmn:sequenceFlow id="g2" sourceRef="t2" targetRef="join"/>
    <bpmn:sequenceFlow id="g3" sourceRef="t3" targetRef="join"/>
    <bpmn:sequenceFlow id="h1" sourceRef="join" targetRef="end"/>
  </bpmn:process>
</bpmn:definitions>
"""


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "camunda_test.db")


def _deploy_loan(engine: ProcessEngine) -> None:
    model = parse_bpmn_file(str(EXAMPLES / "loan-approval.bpmn"))
    engine.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    engine.deploy(model)


def _count(db_path: str, entity) -> int:
    with Store(db_path).session() as s:
        return s.scalar(select(func.count()).select_from(entity))


# ---------------------------------------------------------------------------
# 1. 部署定义恢复
# ---------------------------------------------------------------------------
def test_deployment_survives_restart(db_path):
    engine1 = ProcessEngine(store=Store(db_path))
    _deploy_loan(engine1)
    assert engine1.get_definition_version("loan-approval") == 1

    engine2 = ProcessEngine.from_database(db_path)
    assert engine2.get_definition_version("loan-approval") == 1
    # 恢复后可正常启动并跑完（自动通过路径）
    engine2.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    pi = engine2.start_process_instance_by_key(
        "loan-approval", {"applicant": "王五", "amount": 100}
    )
    assert pi.state.value == "COMPLETED"


def test_deployment_version_increment(db_path):
    e1 = ProcessEngine(store=Store(db_path))
    _deploy_loan(e1)
    _deploy_loan(e1)  # 再次部署同 key
    assert e1.get_definition_version("loan-approval") == 2

    e2 = ProcessEngine.from_database(db_path)
    assert e2.get_definition_version("loan-approval") == 2  # 取最新版本


# ---------------------------------------------------------------------------
# 2. 实例断点续跑（userTask 等待 + 变量）
# ---------------------------------------------------------------------------
def test_instance_resumes_after_restart(db_path):
    # 第一段：部署并启动大额贷款 -> 停在人工审批
    e1 = ProcessEngine(store=Store(db_path))
    _deploy_loan(e1)
    pi1 = e1.start_process_instance_by_key(
        "loan-approval", {"applicant": "赵六", "amount": 20000}
    )
    assert pi1.state.value == "ACTIVE"
    # 落库行数抽查：RU_EXECUTION 应有 ACTIVE token
    assert _count(db_path, HistProcInstEntity) == 1  # HI_PROCINST 已写

    # 模拟崩溃：丢弃 e1（新连接、重新构造引擎）
    e2 = ProcessEngine.from_database(db_path)
    pi2 = e2.get_process_instance(pi1.id)
    assert pi2 is not None
    assert pi2.variables["amount"] == 20000  # 变量持久化
    tasks = e2.create_task_query(process_instance_id=pi1.id)
    assert len(tasks) == 1 and tasks[0].name == "人工审批"

    # 第二段：续跑 -> 完成 -> COMPLETED
    e2.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    e2.complete_task(tasks[0].id, {"approved": True})
    assert pi2.state.value == "COMPLETED"
    assert pi2.end_time is not None

    # 已完成的实例不应再出现在 RU（load_active_instances 只回活跃）
    e3 = ProcessEngine.from_database(db_path)
    assert e3.list_process_instances() == []


def test_hi_tables_written(db_path):
    e1 = ProcessEngine(store=Store(db_path))
    _deploy_loan(e1)
    pi = e1.start_process_instance_by_key(
        "loan-approval", {"applicant": "钱七", "amount": 30000}
    )
    tasks = e1.create_task_query(process_instance_id=pi.id)
    e1.complete_task(tasks[0].id, {"approved": False})

    # HI_PROCINST：COMPLETED + end_time
    with Store(db_path).session() as s:
        hi = s.get(HistProcInstEntity, pi.id)
        assert hi is not None and hi.state_ == "COMPLETED" and hi.end_time_
    # HI_ACTINST：走过的活动都归档
    assert _count(db_path, HistActInstEntity) >= 6
    # HI_TASKINST：人工审批已归档且有 end_time
    with Store(db_path).session() as s:
        row = s.scalar(select(HistTaskInstEntity).where(
            HistTaskInstEntity.task_definition_key_ == "manual-review"
        ))
        assert row is not None and row.end_time_ is not None
    # HI_VARINST：变量快照
    with Store(db_path).session() as s:
        var = s.scalar(select(HistVarInstEntity).where(
            HistVarInstEntity.process_instance_id_ == pi.id,
            HistVarInstEntity.name_ == "approved",
        ))
        assert var is not None and var.text_ == "false"


# ---------------------------------------------------------------------------
# 3. 并行 join 等待恢复
# ---------------------------------------------------------------------------
def test_parallel_join_wait_restored(db_path):
    from camunda.parser import parse_bpmn_xml

    e1 = ProcessEngine(store=Store(db_path))
    e1.deploy(parse_bpmn_xml(FORK3_XML))
    pi = e1.start_process_instance_by_key("fork3", {"k": 1})
    tasks = e1.create_task_query(process_instance_id=pi.id)
    assert len(tasks) == 3

    # 完成 2 个分支 -> 2 个 token 停在 join 等待，1 个任务还挂着
    by_key = {t.task_definition_key: t for t in tasks}
    e1.complete_task(by_key["t1"].id, {"a": 1})
    e1.complete_task(by_key["t2"].id, {"b": 2})
    assert pi.state.value == "ACTIVE"
    assert len(pi.join_arrived("join")) == 2  # 内存 join 等待已登记

    # 崩溃恢复：join_arrivals 应被重建（RU 中停在 join 网关的 ACTIVE token）
    e2 = ProcessEngine.from_database(db_path)
    pi2 = e2.get_process_instance(pi.id)
    assert pi2.state.value == "ACTIVE"
    assert len(pi2.join_arrived("join")) == 2, "join 等待状态未恢复"

    remaining = e2.create_task_query(process_instance_id=pi.id)
    assert len(remaining) == 1 and remaining[0].task_definition_key == "t3"
    e2.complete_task(remaining[0].id, {"c": 3})
    assert pi2.state.value == "COMPLETED", "恢复后第三个分支完成应触发 join 汇聚结束"
