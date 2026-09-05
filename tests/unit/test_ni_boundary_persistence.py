"""M4-2b5：非中断式边界事件（M4-2b4）持久化与崩溃恢复测试。

验证点：
- NI 边界 job 停等落库 -> 重启还原（job/任务/未结算 actinst）-> 到期触发
  并发线（宿主保留、任务不消失、边界 job 消费）-> 宿主完成后实例收束
- root 转 SCOPE 停驻（activity_id=None，主线先完并发线未收）落库 -> 重启
  还原该停驻态 -> 并发线完成后由收尾段完成实例（RU 清空 / HI 归档）
- subProcess 内 NI：宿主完成、sub scope 停驻等待内部并发线 -> 重启还原 ->
  并发线完成后 sub 复活沿出边推进 -> 实例完成
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
        + f'<bpmn:process id="{name}" name="M4-2b-P" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))


def subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, children=inner)


def hi_rows(db: str, table: str, pid: str) -> list:
    import sqlite3

    con = sqlite3.connect(db)
    rows = con.execute(f"SELECT * FROM {table} WHERE PROC_INST_ID_ = ?", (pid,)).fetchall()
    con.close()
    return rows


# start -> taskA(userTask) -> endMain；taskA NI 边界 esc(PT5S)
def ni_host_flow(conc_target: str = "endEsc", stop_task: bool = False) -> str:
    """进程级 userTask 宿主 + 非中断式边界 esc。

    stop_task=False：并发线直通 conc_target（立即收束）；
    stop_task=True ：并发线先停 watchUt(userTask) 再走 conc_target。
    """
    mid = (
        e("userTask", "watchUt", 'name="并发线任务"')
        + f("f2", "esc", "watchUt")
        + f("f3", "watchUt", conc_target)
        if stop_task
        else f("f2", "esc", conc_target)
    )
    return (
        e("startEvent", "start")
        + f("f0", "start", "taskA")
        + e("userTask", "taskA", 'name="主线任务"')
        + e(
            "boundaryEvent",
            "esc",
            'attachedToRef="taskA" cancelActivity="false"',
            timer_evt("duration", "PT5S"),
        )
        + mid
        + f("f1", "taskA", "endMain")
        + e("endEvent", "endMain")
        + e("endEvent", conc_target)
    )


def test_crash_recovery_ni_fires_after_restart(fake_clock, tmp_path):
    """NI 边界停等落库 -> 重启还原 -> 到期触发并发线（宿主保留）-> 宿主完成收束。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    deploy(e1, ni_host_flow(stop_task=False), "recover-ni")
    pi1 = e1.start_process_instance_by_key("recover-ni")
    (task,) = e1.create_task_query()
    (job,) = e1.create_job_query(process_instance_id=pi1.id)
    assert job.job_type == "timer-boundary" and job.node_id == "esc"
    # 「崩溃」重启：job/任务/未结算 actinst 还原
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert pi2.id == pi1.id and not pi2.is_completed
    assert pi2.root_execution.activity_id == "taskA"
    (t2,) = e2.create_task_query()
    assert t2.id == task.id and t2.task_definition_key == "taskA"
    (j2,) = e2.create_job_query(process_instance_id=pi2.id)
    assert j2.id == job.id and j2.job_type == "timer-boundary"
    assert pi2.root_execution.open_activity is not None
    assert pi2.root_execution.open_activity.end_time is None
    # 到期触发：恢复后 NI 分流生效 -> 并发线 spawn 收束，宿主保留、job 消费
    fake_clock.advance(5)
    assert e2.execute_due_jobs() == 1
    assert not e2.get_process_instance(pi2.id).is_completed
    (t_after,) = e2.create_task_query()
    assert t_after.task_definition_key == "taskA"  # host 未取消
    assert e2.create_job_query(process_instance_id=pi2.id) == []  # NI job 已消费
    # 宿主完成 -> 实例收束完成 -> 库一致
    e2.complete_task(t_after.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_task_query() == [] and e2.create_job_query() == []
    assert Store(db).load_active_instances() == []
    assert ProcessEngine.from_database(db).list_process_instances() == []


def test_crash_recovery_root_scope_waiting_concurrent_line(fake_clock, tmp_path):
    """主线先完 root 转 SCOPE 停驻（等并发线）落库 -> 重启还原 -> 并发线完收尾。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    # esc -> watchUt(userTask) -> endEsc：并发线会停等，主线 taskA 先完成
    deploy(e1, ni_host_flow(conc_target="endEsc", stop_task=True), "recover-ni-root")
    pi1 = e1.start_process_instance_by_key("recover-ni-root")
    # 到期触发 NI：taskA + watchUt 双任务（并发线停等）
    fake_clock.advance(5)
    assert e1.execute_due_jobs() == 1
    tasks = {t.task_definition_key: t for t in e1.create_task_query()}
    assert set(tasks) == {"taskA", "watchUt"}
    # 主线 taskA 先完成 -> root 带活跃 child 转 SCOPE 停驻（activity 清空）不完成
    e1.complete_task(tasks["taskA"].id)
    pi_wait = e1.get_process_instance(pi1.id)
    assert not pi_wait.is_completed
    assert pi_wait.root_execution.role == "SCOPE"
    assert pi_wait.root_execution.activity_id is None
    assert len(pi_wait.root_execution.children) == 1
    # 「崩溃」重启：root SCOPE 停驻态（activity_id=None）+ 并发线 TOKEN 还原
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed
    root = pi2.root_execution
    assert root.role == "SCOPE" and root.activity_id is None
    assert len(root.children) == 1
    child = root.children[0]
    assert child.state == ExecutionState.ACTIVE
    assert child.role == "TOKEN" and child.activity_id == "watchUt"
    (w2,) = e2.create_task_query()
    assert w2.task_definition_key == "watchUt"
    assert child.open_activity is not None and child.open_activity.end_time is None
    # 并发线完成 -> 收尾段完成实例 -> RU 清空
    e2.complete_task(w2.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_task_query() == [] and e2.create_job_query() == []
    assert Store(db).load_active_instances() == []
    # HI 一致性：两条 userTask 均正常归档带 end_time（r[2]=TASK_DEF_KEY_, r[7]=END_TIME_）
    hi_tasks = {r[2]: r for r in hi_rows(db, "ACT_HI_TASKINST", pi1.id)}
    assert set(hi_tasks) == {"taskA", "watchUt"}
    assert hi_tasks["taskA"][7] is not None
    assert hi_tasks["watchUt"][7] is not None


def test_crash_recovery_ni_inside_subprocess(fake_clock, tmp_path):
    """sub 内 NI：宿主完成、sub 停驻等内部并发线落库 -> 重启 -> 并发线完 sub 复活出边。"""
    db = str(tmp_path / "camunda.db")
    e1 = ProcessEngine(store=Store(db))
    inner = (
        e("startEvent", "is")
        + f("if0", "is", "inUt")
        + e("userTask", "inUt", 'name="内部主线任务"')
        + e(
            "boundaryEvent",
            "iesc",
            'attachedToRef="inUt" cancelActivity="false"',
            timer_evt("duration", "PT3S"),
        )
        + e("userTask", "iWatch", 'name="内部并发线任务"')
        + f("if1", "inUt", "ie")
        + f("if2", "iesc", "iWatch")
        + f("if3", "iWatch", "iescEnd")
        + e("endEvent", "ie")
        + e("endEvent", "iescEnd")
    )
    deploy(
        e1,
        e("startEvent", "start")
        + f("f1", "start", "sub")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f2", "sub", "end"),
        "recover-ni-sub",
    )
    pi1 = e1.start_process_instance_by_key("recover-ni-sub")
    # 到期触发 sub 内 NI：inUt + iWatch 双任务
    fake_clock.advance(3)
    assert e1.execute_due_jobs() == 1
    tasks = {t.task_definition_key: t for t in e1.create_task_query()}
    assert set(tasks) == {"inUt", "iWatch"}
    # 内部主线 inUt 完成 -> 内部到 ie；sub scope 有活跃并发线 -> 不收、实例不完成
    e1.complete_task(tasks["inUt"].id)
    assert not e1.get_process_instance(pi1.id).is_completed
    assert pi1.root_execution.role == "SCOPE" and pi1.root_execution.activity_id == "sub"
    # 「崩溃」重启：sub scope + 内部并发线 TOKEN 还原
    e2 = ProcessEngine.from_database(db)
    (pi2,) = e2.list_process_instances()
    assert not pi2.is_completed
    root = pi2.root_execution
    assert root.role == "SCOPE" and root.activity_id == "sub"
    assert len(root.children) == 1
    conc = root.children[0]
    assert conc.role == "TOKEN" and conc.activity_id == "iWatch"
    (iw2,) = e2.create_task_query()
    assert iw2.task_definition_key == "iWatch"
    # 并发线完成 -> sub 复活沿出边走 -> 外层完成 -> RU 清空
    e2.complete_task(iw2.id)
    assert e2.get_process_instance(pi2.id).is_completed
    assert e2.create_task_query() == [] and e2.create_job_query() == []
    assert Store(db).load_active_instances() == []
    (sub_hi,) = [r for r in hi_rows(db, "ACT_HI_ACTINST", pi1.id) if r[2] == "sub"]
    assert sub_hi[6] is not None  # sub actinst 正常结算（非中断残留）
