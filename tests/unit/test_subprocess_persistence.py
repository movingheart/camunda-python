"""M4-2a4：embedded SubProcess 持久化与崩溃恢复测试。

验证点：
- 子流程内部 userTask 停等落库 -> 重启还原 execution 树（SCOPE@sub + 内部 TOKEN）
  + 任务 + 未结算 actinst -> complete 后收束完成
- 子流程内部并行 join 等待落库 -> 重启还原 join_arrivals（内部容器节点解析）
  -> 汇聚完成
- subProcess 边界 timer 停等落库 -> 重启还原 -> 到期中断整段 scope -> 库一致
  （RU 清空 / HI_TASKINST 归档带 end_time / sub actinst 结算）
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.engine.process_engine import ProcessEngine
from camunda.model.execution import ExecutionState
from camunda.parser.bpmn_parser import parse_bpmn_xml
from camunda.persistence.store import Store

BPMN_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
BPMN_TAIL = "</bpmn:definitions>\n"


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


def e(tag: str, node_id: str, attrs: str = "", children: str = "") -> str:
    sp = f" {attrs}" if attrs else ""
    if children:
        return f'<bpmn:{tag} id="{node_id}"{sp}>{children}</bpmn:{tag}>'
    return f'<bpmn:{tag} id="{node_id}"{sp}/>'


def f(fid: str, src: str, tgt: str) -> str:
    return f'<bpmn:sequenceFlow id="{fid}" sourceRef="{src}" targetRef="{tgt}"/>'


def timer_evt(kind: str, text: str) -> str:
    k = {"duration": "timeDuration", "date": "timeDate", "cycle": "timeCycle"}[kind]
    return (
        f'<bpmn:timerEventDefinition><bpmn:{k} xsi:type="bpmn:tFormalExpression">'
        f"{text}</bpmn:{k}></bpmn:timerEventDefinition>"
    )


def deploy(engine: ProcessEngine, body: str, name: str) -> None:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M4-2a-P" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))


def hi_rows(db: str, table: str, pid: str) -> list:
    import sqlite3

    con = sqlite3.connect(db)
    rows = con.execute(f"SELECT * FROM {table} WHERE PROC_INST_ID_ = ?", (pid,)).fetchall()
    con.close()
    return rows


def subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, children=inner)


def test_crash_recovery_subprocess_waiting_inside(fake_clock, tmp_path):
    """内部 userTask 停等崩溃 -> 重启还原树/任务/actinst -> complete 收束完成。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    inner = (
        e("startEvent", "is")
        + e("userTask", "innerWait", 'name="内部审批"')
        + e("endEvent", "ie")
        + f("if1", "is", "innerWait")
        + f("if2", "innerWait", "ie")
    )
    deploy(
        e1,
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end"),
        "recover-sub-wait",
    )
    pi1 = e1.start_process_instance_by_key("recover-sub-wait")
    (task,) = e1.create_task_query()
    # 「崩溃」重启
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert pi2.id == pi1.id and not pi2.is_completed
    # execution 树还原：root(SCOPE@sub) -> child(TOKEN@innerWait)
    root = pi2.root_execution
    assert root.role == "SCOPE" and root.activity_id == "sub"
    assert len(root.children) == 1
    child = root.children[0]
    assert child.state == ExecutionState.ACTIVE
    assert child.role == "TOKEN" and child.activity_id == "innerWait"
    # 任务还原 + 未结算 actinst 挂回
    (t2,) = e2.create_task_query()
    assert t2.id == task.id and t2.task_definition_key == "innerWait"
    assert child.open_activity is not None and child.open_activity.end_time is None
    # 完成内部任务 -> 收束完成 -> RU 清空
    e2.complete_task(t2.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_task_query() == [] and e2.create_job_query() == []
    assert Store(db).load_active_instances() == []


def test_crash_recovery_subprocess_parallel_join_waiting(fake_clock, tmp_path):
    """内部并行 join 等待崩溃 -> 重启还原 join 登记（内部容器解析）-> 汇聚完成。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    inner = (
        e("startEvent", "is")
        + e("parallelGateway", "fork")
        + e("userTask", "waitA")
        + e("userTask", "waitB")
        + e("parallelGateway", "join")
        + e("endEvent", "ie")
        + f("if1", "is", "fork")
        + f("if2", "fork", "waitA")
        + f("if3", "fork", "waitB")
        + f("if4", "waitA", "join")
        + f("if5", "waitB", "join")
        + f("if6", "join", "ie")
    )
    deploy(
        e1,
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end"),
        "recover-sub-join",
    )
    pi1 = e1.start_process_instance_by_key("recover-sub-join")
    tasks = e1.create_task_query()
    by_key = {t.task_definition_key: t for t in tasks}
    # 完成 waitA -> waitB 已到 join 等待（停等、未汇聚）
    e1.complete_task(by_key["waitA"].id)
    assert not e1.get_process_instance(pi1.id).is_completed
    # 崩溃重启：内部 join 等待 token 还原
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed
    assert len(pi2.join_arrived("join")) == 1  # waitB 停在内部 join
    (wait_b_task,) = e2.create_task_query()
    assert wait_b_task.task_definition_key == "waitB"
    # 完成 waitB -> 汇聚（2/2）-> 内部走完 -> sub 收束 -> 外层完成
    e2.complete_task(wait_b_task.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert pi2.join_arrivals == {}
    assert Store(db).load_active_instances() == []


def test_crash_recovery_subprocess_boundary_interrupt(fake_clock, tmp_path):
    """subProcess 边界 timer 停等崩溃 -> 重启还原 -> 到期中断整段 scope -> 库一致。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    inner = (
        e("startEvent", "is")
        + e("userTask", "innerWait")
        + e("endEvent", "ie")
        + f("if1", "is", "innerWait")
        + f("if2", "innerWait", "ie")
    )
    deploy(
        e1,
        e("startEvent", "start")
        + subproc("sub", inner)
        + e(
            "boundaryEvent",
            "esc",
            'attachedToRef="sub"',
            timer_evt("duration", "PT3S"),
        )
        + e("endEvent", "end")
        + e("endEvent", "escEnd")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end")
        + f("f3", "esc", "escEnd"),
        "recover-sub-boundary",
    )
    pi1 = e1.start_process_instance_by_key("recover-sub-boundary")
    (job,) = e1.create_job_query(process_instance_id=pi1.id)
    assert job.job_type == "timer-boundary" and job.node_id == "esc"
    # 崩溃重启：树 + 边界 job + 内部任务还原
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed
    assert pi2.root_execution.activity_id == "sub"
    (j2,) = e2.create_job_query(process_instance_id=pi2.id)
    assert j2.id == job.id
    (task,) = e2.create_task_query()
    assert task.task_definition_key == "innerWait"
    # 到期触发：scope 取消 -> 边界路径完成
    fake_clock.advance(3)
    assert e2.execute_due_jobs() == 1
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_task_query() == [] and e2.create_job_query() == []
    assert Store(db).load_active_instances() == []
    # HI 一致性：内部任务中断归档带 end_time；subProcess actinst 结算
    (t_hi,) = hi_rows(db, "ACT_HI_TASKINST", pi1.id)
    assert t_hi[7] is not None
    sub_hi = [r for r in hi_rows(db, "ACT_HI_ACTINST", pi1.id) if r[2] == "sub"]
    assert sub_hi and sub_hi[0][6] is not None
