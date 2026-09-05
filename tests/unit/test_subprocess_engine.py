"""M4-2a2：embedded SubProcess 引擎语义测试（内存模式）。

覆盖：进入/展开（token 转 SCOPE + spawn 内部子 token）、内部停等与 complete、
嵌套子流程递归收束、内部并行 fork/join、内部并行直通 end（SCOPE 逐层收束链）、
子流程内部 userTask 挂边界 timer（M4-1 容器化回归）、带边界事件的 subProcess
实例化明确报错、跨容器同名节点不串扰、实例级变量共享、外层并行 + 内部子流程
组合。持久化/崩溃恢复见 M4-2a4。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from camunda.common import clock
from camunda.engine.process_engine import ProcessEngine
from camunda.model.execution import ExecutionState, ProcessInstanceState
from camunda.parser.bpmn_parser import parse_bpmn_xml

BPMN_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
BPMN_TAIL = "</bpmn:definitions>\n"


class FakeClock:
    """可拨快的 fake 时钟（与 test_job/test_boundary 约定一致）。"""

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


def svc(node_id: str, impl: str) -> str:
    return e("serviceTask", node_id, f'camunda:delegateExpression="${{{impl}}}"')


def subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, children=inner)


def deploy(engine: ProcessEngine, body: str, name: str) -> str:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M4-2a" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))
    return name


# ---------------------------------------------------------------------------
# 进入 / 展开 / 收束
# ---------------------------------------------------------------------------
def test_subprocess_auto_pass_through():
    """单主线直通子流程（内部同步走完）-> 实例一次 pump 即完成。"""
    eng = ProcessEngine()
    calls = []

    def inner_work(v):
        calls.append(1)
        return None

    eng.register_delegate("innerWork", inner_work)
    inner = (
        e("startEvent", "is")
        + svc("isvc", "innerWork")
        + e("endEvent", "ie")
        + f("if1", "is", "isvc")
        + f("if2", "isvc", "ie")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end"),
        "sub-auto",
    )
    pi = eng.start_process_instance_by_key("sub-auto")
    assert pi.state == ProcessInstanceState.COMPLETED
    assert calls == [1]
    # subProcess actinst 跨整段内部执行（已结算）；根 execution 已结束
    acts = {a.activity_id: a for a in pi.activity_history}
    assert "sub" in acts and acts["sub"].end_time is not None
    assert "isvc" in acts and acts["isvc"].end_time is not None
    assert pi.root_execution.state == ExecutionState.ENDED
    # 无泄漏的活跃 execution / 任务 / job
    assert [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE] == []
    assert eng.create_task_query(process_instance_id=pi.id) == []
    assert eng.create_job_query(process_instance_id=pi.id) == []


def test_subprocess_waits_user_task_inside():
    """子流程内部 userTask 停等：树 = root(SCOPE@sub) + child(TOKEN@内部任务)。"""
    eng = ProcessEngine()
    inner = (
        e("startEvent", "is")
        + e("userTask", "it", 'name="内部审批"')
        + e("endEvent", "ie")
        + f("if1", "is", "it")
        + f("if2", "it", "ie")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end"),
        "sub-wait",
    )
    pi = eng.start_process_instance_by_key("sub-wait")
    assert pi.state == ProcessInstanceState.ACTIVE
    root = pi.root_execution
    assert root.role == "SCOPE" and root.activity_id == "sub"
    assert len(root.children) == 1
    child = root.children[0]
    assert child.role == "TOKEN" and child.activity_id == "it"
    # 任务在内部节点上
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "it" and task.execution_id == child.id
    # 完成内部任务 -> 子流程收束 -> 外层完成
    eng.complete_task(task.id, {"ok": True})
    assert pi.state == ProcessInstanceState.COMPLETED
    assert pi.variables.get("ok") is True
    acts = [a.activity_id for a in pi.activity_history]
    assert "sub" in acts and "it" in acts and "ie" in acts


def test_nested_subprocess_recursive_collapse():
    """两层嵌套：内部任务完成后逐层收束（sub2 -> sub1 -> root）。"""
    eng = ProcessEngine()
    sub2 = (
        e("startEvent", "s2s")
        + e("userTask", "deep")
        + e("endEvent", "s2e")
        + f("s2f1", "s2s", "deep")
        + f("s2f2", "deep", "s2e")
    )
    sub1 = (
        e("startEvent", "s1s")
        + subproc("sub2", sub2)
        + e("endEvent", "s1e")
        + f("s1f1", "s1s", "sub2")
        + f("s1f2", "sub2", "s1e")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub1", sub1)
        + e("endEvent", "end")
        + f("f1", "start", "sub1")
        + f("f2", "sub1", "end"),
        "sub-nested",
    )
    pi = eng.start_process_instance_by_key("sub-nested")
    assert pi.state == ProcessInstanceState.ACTIVE
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "deep"
    eng.complete_task(task.id)
    assert pi.state == ProcessInstanceState.COMPLETED
    assert all(
        e.state == ExecutionState.ENDED for e in pi.executions.values()
    ), "嵌套收束后不应残留活跃 execution"


def test_subprocess_inner_parallel_join():
    """子流程内部并行 fork/join：两条分支各执行 delegate 后汇聚收束。"""
    eng = ProcessEngine()
    calls = []

    def mk(name):
        def fn(v):
            calls.append(name)
            return None

        return fn

    eng.register_delegate("svcA", mk("A"))
    eng.register_delegate("svcB", mk("B"))
    inner = (
        e("startEvent", "is")
        + e("parallelGateway", "fork")
        + svc("ia", "svcA")
        + svc("ib", "svcB")
        + e("parallelGateway", "join")
        + e("endEvent", "ie")
        + f("if1", "is", "fork")
        + f("if2", "fork", "ia")
        + f("if3", "fork", "ib")
        + f("if4", "ia", "join")
        + f("if5", "ib", "join")
        + f("if6", "join", "ie")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end"),
        "sub-par-join",
    )
    pi = eng.start_process_instance_by_key("sub-par-join")
    assert pi.state == ProcessInstanceState.COMPLETED
    assert sorted(calls) == ["A", "B"]
    assert pi.join_arrivals == {}  # join 登记已清空
    acts = [a.activity_id for a in pi.activity_history]
    assert "fork" in acts and "join" in acts and "ia" in acts and "ib" in acts


def test_subprocess_inner_parallel_direct_to_end():
    """子流程内部并行分支各自直通 end（无 join）：SCOPE 逐层收束链。

    内部 fork SCOPE（分支全 end）-> 结束 detach -> subProcess SCOPE 复活 ->
    沿 sub 出边走完外层。验证 collapse 泛化到中间层（M1 只收 root）。
    """
    eng = ProcessEngine()
    calls = []

    def mk(name):
        def fn(v):
            calls.append(name)
            return None

        return fn

    eng.register_delegate("svcA", mk("A"))
    eng.register_delegate("svcB", mk("B"))
    inner = (
        e("startEvent", "is")
        + e("parallelGateway", "fork")
        + svc("ia", "svcA")
        + svc("ib", "svcB")
        + e("endEvent", "ieA")
        + e("endEvent", "ieB")
        + f("if1", "is", "fork")
        + f("if2", "fork", "ia")
        + f("if3", "fork", "ib")
        + f("if4", "ia", "ieA")
        + f("if5", "ib", "ieB")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end"),
        "sub-par-direct",
    )
    pi = eng.start_process_instance_by_key("sub-par-direct")
    assert pi.state == ProcessInstanceState.COMPLETED
    assert sorted(calls) == ["A", "B"]
    assert all(
        e.state == ExecutionState.ENDED for e in pi.executions.values()
    ), "并行直通 end 后应无活跃 execution 残留"
    # subProcess actinst 已结算（内部并行全结束后才离开）
    sub_ai = next(a for a in pi.activity_history if a.activity_id == "sub")
    assert sub_ai.end_time is not None


def test_subprocess_variables_shared_instance_level():
    """内部 delegate 写实例级变量 -> 外层排他网关据此选路（无子作用域遮蔽）。"""
    eng = ProcessEngine()

    def set_flag(v):
        v["flag"] = True
        return None

    eng.register_delegate("setFlag", set_flag)
    inner = (
        e("startEvent", "is")
        + svc("isvc", "setFlag")
        + e("endEvent", "ie")
        + f("if1", "is", "isvc")
        + f("if2", "isvc", "ie")
    )
    body = (
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("exclusiveGateway", "gw", 'default="flow-low"')
        + e("endEvent", "highEnd")
        + e("endEvent", "lowEnd")
        + f("f1", "start", "sub")
        + f("f2", "sub", "gw")
        + (
            '<bpmn:sequenceFlow id="flow-high" sourceRef="gw" targetRef="highEnd">'
            '<bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">'
            '${flag == true}</bpmn:conditionExpression></bpmn:sequenceFlow>'
        )
        + f("flow-low", "gw", "lowEnd")
    )
    deploy(eng, body, "sub-var")
    pi = eng.start_process_instance_by_key("sub-var")
    assert pi.state == ProcessInstanceState.COMPLETED
    acts = [a.activity_id for a in pi.activity_history]
    assert "highEnd" in acts and "lowEnd" not in acts


def test_parallel_outer_branch_with_subprocess():
    """外层并行：A 分支进子流程停内部任务，B 分支直通 end。

    验证 B 分支 end 触发的 collapse 扫描不会误收 A 分支内部停等主线
    （join 恢复 role 复位 + collapse 只收容器停驻 SCOPE 的双重防御）。
    """
    eng = ProcessEngine()

    def svcB(v):
        return None

    eng.register_delegate("svcB", svcB)
    inner = (
        e("startEvent", "is")
        + e("userTask", "innerWait")
        + e("endEvent", "ie")
        + f("if1", "is", "innerWait")
        + f("if2", "innerWait", "ie")
    )
    body = (
        e("startEvent", "start")
        + e("parallelGateway", "fork")
        + subproc("subA", inner)
        + svc("bSvc", "svcB")
        + e("endEvent", "endA")
        + e("endEvent", "endB")
        + f("f1", "start", "fork")
        + f("f2", "fork", "subA")
        + f("f3", "fork", "bSvc")
        + f("f4", "subA", "endA")
        + f("f5", "bSvc", "endB")
    )
    deploy(eng, body, "outer-par-sub")
    pi = eng.start_process_instance_by_key("outer-par-sub")
    # B 分支已直通 end；A 分支停在子流程内部任务等待 -> 实例仍 ACTIVE
    assert pi.state == ProcessInstanceState.ACTIVE
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "innerWait"
    # A 分支内部主线仍是 ACTIVE（未被外层 collapse 误收）
    waiting = [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE]
    assert any(e.activity_id == "innerWait" for e in waiting)
    # 完成内部任务 -> A 分支收束 -> root(fork SCOPE) 全子结束 -> 实例完成
    eng.complete_task(task.id)
    assert pi.state == ProcessInstanceState.COMPLETED


def test_boundary_timer_inside_subprocess(fake_clock):
    """子流程内部 userTask 挂边界 timer（M4-1 容器化回归）：中断后收束外层。"""
    eng = ProcessEngine()
    inner = (
        e("startEvent", "is")
        + e("userTask", "innerWait")
        + e(
            "boundaryEvent",
            "esc",
            'attachedToRef="innerWait"',
            timer_evt("duration", "PT1S"),
        )
        + e("endEvent", "ie")
        + e("endEvent", "escEnd")
        + f("if1", "is", "innerWait")
        + f("if2", "esc", "escEnd")
    )
    body = (
        e("startEvent", "start")
        + subproc("sub", inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end")
    )
    deploy(eng, body, "sub-b-inner")
    pi = eng.start_process_instance_by_key("sub-b-inner")
    assert pi.state == ProcessInstanceState.ACTIVE
    # 内部 userTask 停等 -> 边界 timer 作业已注册（容器化：host 在内部容器）
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-boundary" and job.node_id == "esc"
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    # 到期触发：内部宿主取消 -> esc 路径 -> 内部 end -> sub 收束 -> 外层完成
    fake_clock.advance(1)
    assert eng.execute_due_jobs() == 1
    assert pi.state == ProcessInstanceState.COMPLETED
    acts = [a.activity_id for a in pi.activity_history]
    assert "esc" in acts and "escEnd" in acts
    assert [t for t in pi.completed_tasks if t.task_definition_key == "innerWait"]


def test_boundary_timer_interrupts_subprocess(fake_clock):
    """M4-2a3：subProcess 上的边界 timer 到期 -> 整段 scope 取消走边界路径。"""
    eng = ProcessEngine()
    inner = (
        e("startEvent", "is")
        + e("userTask", "innerWait")
        + e("endEvent", "ie")
        + f("if1", "is", "innerWait")
        + f("if2", "innerWait", "ie")
    )
    body = (
        e("startEvent", "start")
        + subproc("sub", inner)
        + e(
            "boundaryEvent",
            "esc",
            'attachedToRef="sub"',
            timer_evt("duration", "PT1S"),
        )
        + e("endEvent", "end")
        + e("endEvent", "escEnd")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end")
        + f("f3", "esc", "escEnd")
    )
    deploy(eng, body, "sub-boundary-interrupt")
    pi = eng.start_process_instance_by_key("sub-boundary-interrupt")
    assert pi.state == ProcessInstanceState.ACTIVE
    # 进入等待窗口：subProcess 边界 timer 已注册；内部任务停等
    (job,) = eng.create_job_query(process_instance_id=pi.id)
    assert job.job_type == "timer-boundary" and job.node_id == "esc"
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "innerWait"
    # 到期触发：取消整段 scope -> 边界 esc 路径走完 -> 外层 escEnd 完成
    fake_clock.advance(1)
    assert eng.execute_due_jobs() == 1
    assert pi.state == ProcessInstanceState.COMPLETED
    acts = [a.activity_id for a in pi.activity_history]
    assert "esc" in acts and "escEnd" in acts and "end" not in acts
    # 内部任务中断归档；subProcess actinst 已结算；无泄漏活跃 execution/job
    assert [t for t in pi.completed_tasks if t.task_definition_key == "innerWait"]
    sub_ai = next(a for a in pi.activity_history if a.activity_id == "sub")
    assert sub_ai.end_time is not None
    assert eng.create_task_query(process_instance_id=pi.id) == []
    assert eng.create_job_query(process_instance_id=pi.id) == []


def test_boundary_interrupts_subprocess_inner_parallel(fake_clock):
    """中断进行中的内部并行：两条分支任务都归档、join 登记清空、无泄漏。"""
    eng = ProcessEngine()
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
    body = (
        e("startEvent", "start")
        + subproc("sub", inner)
        + e(
            "boundaryEvent",
            "esc",
            'attachedToRef="sub"',
            timer_evt("duration", "PT2S"),
        )
        + e("endEvent", "end")
        + e("endEvent", "escEnd")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end")
        + f("f3", "esc", "escEnd")
    )
    deploy(eng, body, "sub-boundary-par")
    pi = eng.start_process_instance_by_key("sub-boundary-par")
    assert pi.state == ProcessInstanceState.ACTIVE
    tasks = eng.create_task_query(process_instance_id=pi.id)
    assert {t.task_definition_key for t in tasks} == {"waitA", "waitB"}
    # 两条分支各停 userTask（join 尚未汇聚）
    fake_clock.advance(2)
    assert eng.execute_due_jobs() == 1
    assert pi.state == ProcessInstanceState.COMPLETED
    # 两条分支任务都中断归档
    archived = {t.task_definition_key for t in pi.completed_tasks}
    assert {"waitA", "waitB"} <= archived
    # join 登记清空、无活跃 execution/任务/作业泄漏
    assert pi.join_arrivals == {}
    assert eng.create_task_query(process_instance_id=pi.id) == []
    assert eng.create_job_query(process_instance_id=pi.id) == []
    acts = [a.activity_id for a in pi.activity_history]
    assert "escEnd" in acts and "end" not in acts


def test_boundary_dropped_after_subprocess_completes(fake_clock):
    """subProcess 正常快速完成后其边界 timer 撤销（后续到期无副作用）。"""
    eng = ProcessEngine()
    inner = (
        e("startEvent", "is")
        + e("endEvent", "ie")
        + f("if1", "is", "ie")
    )
    body = (
        e("startEvent", "start")
        + subproc("sub", inner)
        + e(
            "boundaryEvent",
            "esc",
            'attachedToRef="sub"',
            timer_evt("duration", "PT5S"),
        )
        + e("endEvent", "end")
        + e("endEvent", "escEnd")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end")
        + f("f3", "esc", "escEnd")
    )
    deploy(eng, body, "sub-boundary-complete")
    # 内部同步走完：一次启动即完成，边界 timer 随正常离开撤销
    pi = eng.start_process_instance_by_key("sub-boundary-complete")
    assert pi.state == ProcessInstanceState.COMPLETED
    assert eng.create_job_query(process_instance_id=pi.id) == []
    acts = [a.activity_id for a in pi.activity_history]
    assert "end" in acts and "escEnd" not in acts
    fake_clock.advance(5)
    assert eng.execute_due_jobs() == 0  # 无残留作业


def test_same_node_id_across_containers():
    """跨容器同名节点 id：complete 按任务所属 execution 容器解析，不串扰。"""
    eng = ProcessEngine()
    inner = (
        e("startEvent", "is")
        + e("userTask", "task")  # 与外部同名
        + e("endEvent", "ie")
        + f("if1", "is", "task")
        + f("if2", "task", "ie")
    )
    body = (
        e("startEvent", "start")
        + e("userTask", "task", 'name="外层同名任务"')  # 外层同名
        + subproc("sub", inner)
        + e("endEvent", "endOuter")
        + e("endEvent", "endInner")
        + f("f1", "start", "task")
        + f("f2", "task", "endOuter")
        + f("f3", "sub", "endInner")
    )
    deploy(eng, body, "same-id")
    pi = eng.start_process_instance_by_key("same-id")
    # 外层 token 停外层 task（sub 未进入），只有 1 个任务
    (task,) = eng.create_task_query(process_instance_id=pi.id)
    assert task.task_definition_key == "task"
    eng.complete_task(task.id)
    # 应走外层 endOuter 完成；sub 从未进入
    assert pi.state == ProcessInstanceState.COMPLETED
    acts = [a.activity_id for a in pi.activity_history]
    assert "endOuter" in acts and "endInner" not in acts
