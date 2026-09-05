"""M4-2c4：多实例持久化与崩溃恢复测试。

验证点：
- ACT_RU_EXECUTION.MI_ 列落库：容器行 JSON（total/completed/next_index/...）、
  并行实例行 {"index": i}（直查 sqlite）
- 并行 userTask 宿主崩溃：容器状态（completed/active/next_index）与剩余实例
  （{"index": 1/2}）还原 -> 继续完成收束 -> RU 清空
- 顺序 userTask 宿主崩溃：root 容器（sequential）还原 + 当前等待实例在等，
  loopCounter 随变量表还原 -> 完成后续跑下一实例（loopCounter 递增）
- 并行 subProcess 宿主崩溃：树形还原（容器 SCOPE + N 实例 SCOPE@sub + 各自
  内部停等 token/任务）-> 逐实例完成收束
- 顺序 subProcess 宿主崩溃：容器自身 SCOPE@sub 兼实例载体还原 -> 完成当前
  实例自动续跑下一实例（内部新任务/新 actinst）
- completionCondition 崩溃在条件满足前 -> 重启后完成触发条件 -> 剩余实例
  取消归档（HI_TASKINST 全带 end_time）、无孤儿 RU
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.engine.process_engine import ProcessEngine
from camunda.model.execution import ExecutionState
from camunda.parser.bpmn_parser import parse_bpmn_xml
from camunda.persistence.store import Store

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
"""


def wrap(process_xml: str) -> str:
    return HEAD + process_xml + "\n</bpmn:definitions>\n"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime.now().replace(microsecond=0)

    def now(self) -> str:
        return self.t.strftime("%Y-%m-%dT%H:%M:%S")

    def advance(self, seconds: float) -> str:
        self.t += timedelta(seconds=seconds)
        return self.now()


@pytest.fixture(autouse=True)
def fake_clock() -> FakeClock:
    fc = FakeClock()
    clock.set_clock(fc.now)
    yield fc
    clock.reset_clock()


def mi_loop(
    sequential: bool = False,
    collection: str | None = "${reviewers}",
    element_variable: str | None = "reviewer",
    cardinality: str | None = None,
    completion: str | None = None,
) -> str:
    seq = ' isSequential="true"' if sequential else ""
    col_attr = f' camunda:collection="{collection}"' if collection else ""
    ev_attr = (
        f' camunda:elementVariable="{element_variable}"' if element_variable else ""
    )
    card = (
        f'<bpmn:loopCardinality xsi:type="bpmn:tFormalExpression">{cardinality}</bpmn:loopCardinality>'
        if cardinality
        else ""
    )
    comp = (
        f'<bpmn:completionCondition xsi:type="bpmn:tFormalExpression">{completion}</bpmn:completionCondition>'
        if completion
        else ""
    )
    return (
        f'<bpmn:multiInstanceLoopCharacteristics{seq}{col_attr}{ev_attr}>'
        f"{card}{comp}"
        f"</bpmn:multiInstanceLoopCharacteristics>"
    )


def mi_user_task(loop: str) -> str:
    return f'<bpmn:userTask id="review" name="会签">{loop}</bpmn:userTask>'


def mi_subprocess(node_id: str, inner: str, loop: str) -> str:
    return (
        f'<bpmn:subProcess id="{node_id}">{loop}{inner}</bpmn:subProcess>'
    )


def inner_wait_flow() -> str:
    return (
        '<bpmn:startEvent id="is"/>'
        '<bpmn:userTask id="innerTask" name="内审"/>'
        '<bpmn:endEvent id="ie"/>'
        '<bpmn:sequenceFlow id="if1" sourceRef="is" targetRef="innerTask"/>'
        '<bpmn:sequenceFlow id="if2" sourceRef="innerTask" targetRef="ie"/>'
    )


def deploy_flow(engine: ProcessEngine, key: str, mi: str, node_id: str) -> None:
    xml = wrap(
        f"""
  <bpmn:process id="{key}" name="MI-P" isExecutable="true">
    <bpmn:startEvent id="start"/>
    {mi}
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="{node_id}"/>
    <bpmn:sequenceFlow id="f2" sourceRef="{node_id}" targetRef="end"/>
  </bpmn:process>
"""
    )
    assert key in engine.deploy(parse_bpmn_xml(xml, source_name=key))


def hi_rows(db: str, table: str, pid: str) -> list:
    import sqlite3

    con = sqlite3.connect(db)
    rows = con.execute(
        f"SELECT * FROM {table} WHERE PROC_INST_ID_ = ?", (pid,)
    ).fetchall()
    con.close()
    return rows


def ru_exec_mi_rows(db: str, pid: str) -> list:
    """直查 ACT_RU_EXECUTION 的 (ID_, ROLE_, MI_) 三元组（验证落库内容）。"""
    import sqlite3

    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT ID_, ROLE_, MI_ FROM ACT_RU_EXECUTION WHERE PROC_INST_ID_ = ?",
        (pid,),
    ).fetchall()
    con.close()
    return rows


def test_crash_recovery_parallel_user_task_mi_state(fake_clock, tmp_path):
    """并行 userTask：完成 1 个崩溃 -> 容器状态/剩余实例/任务还原 -> 完成收束。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy_flow(e1, "mi-par-persist", mi_user_task(mi_loop()), "review")
    pi1 = e1.start_process_instance_by_key(
        "mi-par-persist", {"reviewers": ["a", "b", "c"]}
    )
    tasks = e1.create_task_query()
    assert len(tasks) == 3
    # 完成第 1 个实例 -> 崩溃
    e1.complete_task(tasks[0].id)
    root1 = e1.get_process_instance(pi1.id).root_execution
    assert root1.is_mi_container and root1.mi["completed"] == 1
    # RU MI_ 列直查：容器行 JSON 含 total/completed；剩余实例行 {"index": 1/2}
    rows = ru_exec_mi_rows(db, pi1.id)
    assert len(rows) == 3  # 容器 + 2 条活跃实例 child（完成实例已 detach 不落库）
    import json

    container_row = [r for r in rows if r[1] == "SCOPE"]
    assert len(container_row) == 1
    mi_c = json.loads(container_row[0][2])
    assert mi_c["total"] == 3 and mi_c["completed"] == 1 and mi_c["next_index"] == 3
    assert mi_c["sequential"] is False and mi_c["active"] == 2
    child_mis = {json.loads(r[2])["index"] for r in rows if r[1] == "TOKEN"}
    assert child_mis == {1, 2}
    # 崩溃重启：容器状态与剩余实例树还原
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed
    root = pi2.root_execution
    assert root.role == "SCOPE" and root.activity_id == "review"
    assert root.is_mi_container
    assert root.mi["total"] == 3 and root.mi["completed"] == 1
    assert root.mi["active"] == 2 and root.mi["next_index"] == 3
    assert len(root.children) == 2
    assert {c.mi["index"] for c in root.children} == {1, 2}
    assert all(
        c.role == "TOKEN" and c.activity_id == "review"
        and c.open_activity is not None and c.open_activity.end_time is None
        for c in root.children
    )
    remaining = e2.create_task_query()
    assert {t.task_definition_key for t in remaining} == {"review"}
    assert len(remaining) == 2
    # 完成剩余实例 -> 容器收束离开 -> RU 清空
    for t in remaining:
        e2.complete_task(t.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert root.mi is None and root.role == "TOKEN"
    assert e2.create_task_query() == [] and e2.create_job_query() == []
    assert Store(db).load_active_instances() == []


def test_crash_recovery_sequential_user_task_resumes_next(fake_clock, tmp_path):
    """顺序 userTask：完成第 1 个后崩溃 -> 容器还原 -> 续跑第 3 实例（loopCounter 递增）。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy_flow(
        e1,
        "mi-seq-persist",
        mi_user_task(
            mi_loop(sequential=True, collection=None, element_variable=None, cardinality="3")
        ),
        "review",
    )
    pi1 = e1.start_process_instance_by_key("mi-seq-persist")
    assert pi1.variables["loopCounter"] == 0  # 第 1 实例注入
    (t1,) = e1.create_task_query()
    e1.complete_task(t1.id)
    # 同命令内自动续跑：第 2 实例任务已建（loopCounter=1）并落库
    (t2,) = e1.create_task_query()
    assert e1.get_process_instance(pi1.id).variables["loopCounter"] == 1
    assert e1.get_process_instance(pi1.id).root_execution.mi["completed"] == 1
    # 崩溃重启：root（顺序容器）状态还原 + 当前实例任务在等
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    root = pi2.root_execution
    assert not pi2.is_completed
    assert root.is_mi_container and root.mi["sequential"] is True
    assert root.mi["total"] == 3 and root.mi["completed"] == 1
    assert root.mi["next_index"] == 2 and root.mi["active"] == 1
    assert root.role == "TOKEN" and root.activity_id == "review"
    assert root.open_activity is not None and root.open_activity.end_time is None
    # loopCounter 随变量表还原 = 当前等待实例序号（第 2 实例 index=1）
    assert pi2.variables["loopCounter"] == 1
    (t2r,) = e2.create_task_query()
    assert t2r.id == t2.id
    # 完成第 2 实例 -> 续跑第 3 实例（loopCounter=2）-> 完成收束
    e2.complete_task(t2r.id)
    assert e2.get_process_instance(pi2.id).variables["loopCounter"] == 2
    (t3,) = e2.create_task_query()
    e2.complete_task(t3.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert root.mi is None
    assert e2.create_task_query() == []
    assert Store(db).load_active_instances() == []
    # 历史：3 条 userTask actinst 全结算（宿主跨 3 实例各一条）
    acts = [r for r in hi_rows(db, "ACT_HI_ACTINST", pi1.id) if r[2] == "review"]
    assert len(acts) == 3 and all(r[6] is not None for r in acts)


def test_crash_recovery_parallel_subprocess_host(fake_clock, tmp_path):
    """并行 subProcess 宿主：完成 1 实例内部崩溃 -> 树形还原 -> 逐实例收束。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy_flow(
        e1,
        "mi-sub-par-persist",
        mi_subprocess("sub", inner_wait_flow(), mi_loop()),
        "sub",
    )
    pi1 = e1.start_process_instance_by_key(
        "mi-sub-par-persist", {"reviewers": ["a", "b", "c"]}
    )
    tasks = e1.create_task_query()
    assert len(tasks) == 3 and {t.task_definition_key for t in tasks} == {"innerTask"}
    # 完成实例 A（index 0）内部任务 -> 其实例载体收束 detach，容器 active 2
    e1.complete_task(tasks[0].id)
    root1 = e1.get_process_instance(pi1.id).root_execution
    assert root1.mi["completed"] == 1 and root1.mi["active"] == 2
    assert len(root1.children) == 2
    # 崩溃重启
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed
    root = pi2.root_execution
    # 树：容器 SCOPE@sub -> 2 实例载体 SCOPE@sub（各含内部停等 TOKEN）
    assert root.role == "SCOPE" and root.activity_id == "sub"
    assert root.is_mi_container
    assert root.mi["total"] == 3 and root.mi["completed"] == 1
    assert root.mi["active"] == 2 and root.mi["next_index"] == 3
    assert len(root.children) == 2
    for inst in root.children:
        assert inst.role == "SCOPE" and inst.activity_id == "sub"
        assert inst.mi is not None and "index" in inst.mi and inst.mi["index"] in (1, 2)
        assert len(inst.children) == 1
        inner = inst.children[0]
        assert inner.role == "TOKEN" and inner.activity_id == "innerTask"
        assert inner.open_activity is not None and inner.open_activity.end_time is None
    remaining = e2.create_task_query()
    assert len(remaining) == 2
    # 完成剩余两个实例的内部任务 -> 容器收束离开
    for t in remaining:
        e2.complete_task(t.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert root.mi is None and root.role == "TOKEN"
    assert e2.create_task_query() == []
    assert Store(db).load_active_instances() == []


def test_crash_recovery_sequential_subprocess_host_resumes(fake_clock, tmp_path):
    """顺序 subProcess 宿主：实例 1 内部完成后崩溃 -> 容器续跑实例 3。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy_flow(
        e1,
        "mi-sub-seq-persist",
        mi_subprocess(
            "sub",
            inner_wait_flow(),
            mi_loop(sequential=True, collection=None, element_variable=None, cardinality="3"),
        ),
        "sub",
    )
    pi1 = e1.start_process_instance_by_key("mi-sub-seq-persist")
    (t1,) = e1.create_task_query()
    e1.complete_task(t1.id)
    # 实例 1 收束 + 自动续跑实例 2：root 转回 SCOPE@sub 承载实例 2 内部流转
    root1 = e1.get_process_instance(pi1.id).root_execution
    assert root1.is_mi_container and root1.mi["sequential"] is True
    assert root1.mi["completed"] == 1 and root1.mi["active"] == 1
    assert root1.mi["next_index"] == 2
    assert root1.role == "SCOPE" and root1.activity_id == "sub"
    (t2,) = e1.create_task_query()
    # 崩溃重启
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed
    root = pi2.root_execution
    assert root.is_mi_container and root.mi["sequential"] is True
    assert root.mi["completed"] == 1 and root.mi["active"] == 1
    assert root.mi["next_index"] == 2 and root.mi["total"] == 3
    # 容器兼当前实例载体：SCOPE@sub + 内部停等 token（实例 2）
    assert root.role == "SCOPE" and root.activity_id == "sub"
    assert len(root.children) == 1
    inner = root.children[0]
    assert inner.role == "TOKEN" and inner.activity_id == "innerTask"
    assert pi2.variables["loopCounter"] == 1
    (t2r,) = e2.create_task_query()
    assert t2r.id == t2.id
    # 完成实例 2 -> 续跑实例 3（loopCounter=2）-> 完成收束
    e2.complete_task(t2r.id)
    assert e2.get_process_instance(pi2.id).variables["loopCounter"] == 2
    (t3,) = e2.create_task_query()
    e2.complete_task(t3.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert root.mi is None and root.role == "TOKEN"
    assert e2.create_task_query() == []
    assert Store(db).load_active_instances() == []


def test_crash_recovery_completion_condition_fires_after_restart(fake_clock, tmp_path):
    """completionCondition：条件满足前崩溃 -> 重启后完成触发 -> 剩余实例取消归档。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy_flow(
        e1,
        "mi-cond-persist",
        mi_user_task(mi_loop(completion="${nrOfCompletedInstances >= 2}")),
        "review",
    )
    pi1 = e1.start_process_instance_by_key(
        "mi-cond-persist", {"reviewers": ["a", "b", "c"]}
    )
    tasks = e1.create_task_query()
    assert len(tasks) == 3
    # 完成 1 个（1>=2 不满足）-> 崩溃重启
    e1.complete_task(tasks[0].id)
    assert not e1.get_process_instance(pi1.id).is_completed
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    root = pi2.root_execution
    assert root.is_mi_container and root.mi["completed"] == 1
    remaining = e2.create_task_query()
    assert len(remaining) == 2
    # 完成第 2 个 -> 条件满足 -> 第 3 实例被取消、容器离开 -> 实例完成
    e2.complete_task(remaining[0].id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_task_query() == []  # 第 3 实例任务已随实例取消归档
    assert root.mi is None and root.role == "TOKEN"
    assert Store(db).load_active_instances() == []
    # HI 一致性：3 条任务全归档带 end_time（2 条完成 + 1 条被终止）；actinst 结算
    t_rows = hi_rows(db, "ACT_HI_TASKINST", pi1.id)
    assert len(t_rows) == 3 and all(r[7] is not None for r in t_rows)
    acts = [r for r in hi_rows(db, "ACT_HI_ACTINST", pi1.id) if r[2] == "review"]
    assert len(acts) == 3 and all(r[6] is not None for r in acts)
    # 被终止实例无孤儿 execution 残留（RU 清空已断言；再直查库行）
    assert Store(db).load_active_instances() == []
