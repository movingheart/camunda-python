"""M4-2b2：error end 抛出 + 冒泡 + 中断式事件子流程（error start）引擎测试。

覆盖：流程级 error 事件子流程中断实例、subProcess 级吞错后主流程继续、
error code 不匹配冒泡到外层容器、无捕获等同 none end、root 到 error end 的
接管特判、事件子流程内部再抛错冒泡到外层。timer/message start 事件子流程见
test_timer_event_subprocess.py（M4-2b3）。持久化见 M4-2b5。
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


def svc(node_id: str, impl: str) -> str:
    return e("serviceTask", node_id, f'camunda:delegateExpression="${{{impl}}}"')


def subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, children=inner)


def event_subproc(node_id: str, inner: str) -> str:
    return e("subProcess", node_id, 'triggeredByEvent="true"', inner)


def error_start(node_id: str, code: str, interrupting: bool = True) -> str:
    attr = "" if interrupting else ' isInterrupting="false"'
    return e(
        "startEvent",
        node_id,
        attr,
        f'<bpmn:errorEventDefinition errorCode="{code}"/>',
    )


def error_end(node_id: str, code: str) -> str:
    return e(
        "endEvent",
        node_id,
        children=f'<bpmn:errorEventDefinition errorCode="{code}"/>',
    )


def deploy(engine: ProcessEngine, body: str, name: str) -> str:
    xml = (
        BPMN_HEAD
        + f'<bpmn:process id="{name}" name="M4-2b" isExecutable="true">'
        + body
        + "</bpmn:process>"
        + BPMN_TAIL
    )
    assert name in engine.deploy(parse_bpmn_xml(xml, source_name=name))
    return name


def task_ids(engine: ProcessEngine, pi) -> set[str]:
    return {t.task_definition_key for t in engine.create_task_query(pi.id)}


def open_actinst_of(pi, activity_id: str) -> list:
    return [
        a
        for a in pi.activity_history
        if a.activity_id == activity_id and a.end_time is None
    ]


# ---------------------------------------------------------------------------
# 流程级 error 事件子流程（中断整个实例）
# ---------------------------------------------------------------------------
def test_process_level_error_subprocess_interrupts_instance():
    """主线 error end 抛 ERR_1 -> 流程级事件子流程接管 -> 收尾完成实例。"""
    eng = ProcessEngine()
    esc_inner = (
        error_start("es-start", "ERR_1")
        + e("userTask", "esUt")
        + e("endEvent", "esEnd")
        + f("ef1", "es-start", "esUt")
        + f("ef2", "esUt", "esEnd")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + e("userTask", "mainUt")
        + error_end("boom", "ERR_1")
        + f("f1", "start", "mainUt")
        + f("f2", "mainUt", "boom")
        + event_subproc("esc", esc_inner),
        "err-top",
    )
    pi = eng.start_process_instance_by_key("err-top")
    assert task_ids(eng, pi) == {"mainUt"}

    # 主线任务完成 -> error end -> 事件子流程中断接管
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert task_ids(eng, pi) == {"esUt"}
    assert pi.state == ProcessInstanceState.ACTIVE
    # 事件子流程 scope 停驻在 esc 节点（SCOPE + activity=esc）
    scopes = [
        ex
        for ex in pi.executions.values()
        if ex.role == "SCOPE" and ex.activity_id == "esc"
    ]
    assert len(scopes) == 1
    # 中断清理：主线 ut 的 actinst 已结算、任务已归档留痕
    assert open_actinst_of(pi, "mainUt") == []
    assert {t.task_definition_key for t in pi.completed_tasks} == {"mainUt"}
    assert open_actinst_of(pi, "boom") == []  # error end 已结算

    # 事件子流程任务完成 -> 收尾 -> 实例完成
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED
    assert {t.task_definition_key for t in pi.completed_tasks} == {"mainUt", "esUt"}


# ---------------------------------------------------------------------------
# subProcess 级 error 事件子流程（吞错后主流程继续）
# ---------------------------------------------------------------------------
def test_subprocess_level_error_caught_then_main_continues():
    """subProcess 内抛错 -> 内部事件子流程吞掉 -> sub 正常完成沿出边继续。"""
    eng = ProcessEngine()
    esc_inner = (
        error_start("es-start", "ERR_1")
        + e("userTask", "esUt")
        + e("endEvent", "esEnd")
        + f("ef1", "es-start", "esUt")
        + f("ef2", "esUt", "esEnd")
    )
    sub_inner = (
        e("startEvent", "is")
        + e("userTask", "innerUt")
        + error_end("iboom", "ERR_1")
        + f("if1", "is", "innerUt")
        + f("if2", "innerUt", "iboom")
        + event_subproc("esc", esc_inner)
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", sub_inner)
        + e("userTask", "afterUt")
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "afterUt")
        + f("f3", "afterUt", "end"),
        "err-in-sub",
    )
    pi = eng.start_process_instance_by_key("err-in-sub")
    assert task_ids(eng, pi) == {"innerUt"}

    # 内部任务完成 -> error 抛 -> 同容器事件子流程捕获（subProcess 级）
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert task_ids(eng, pi) == {"esUt"}  # 事件子流程任务停等，主线未继续
    # sub 本体 SCOPE 仍停驻（保留收束复活）；事件子流程 scope 挂其下
    sub_scope = [ex for ex in pi.executions.values() if ex.activity_id == "sub"]
    assert len(sub_scope) == 1 and sub_scope[0].role == "SCOPE"
    # 中断清理：innerUt 任务归档（留痕），错误 end actinst 结算
    assert {t.task_definition_key for t in pi.completed_tasks} == {"innerUt"}

    # 事件子流程收尾 -> sub 收束复活 -> 主线继续到 afterUt
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert task_ids(eng, pi) == {"afterUt"}
    assert open_actinst_of(pi, "sub") == []  # sub actinst 已结算
    assert pi.state == ProcessInstanceState.ACTIVE

    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


# ---------------------------------------------------------------------------
# 错误冒泡：内层 code 不匹配 -> 外层容器捕获
# ---------------------------------------------------------------------------
def test_error_bubbles_to_process_level_when_inner_not_match():
    """sub 内抛 ERR_AAA：sub 级事件子流程只接 ERR_BBB -> 冒泡到流程级接管。"""
    eng = ProcessEngine()
    esc_inner_sub = (
        error_start("es-sub-start", "ERR_BBB")
        + e("userTask", "esSubUt")
        + e("endEvent", "esSubEnd")
        + f("a1", "es-sub-start", "esSubUt")
        + f("a2", "esSubUt", "esSubEnd")
    )
    sub_inner = (
        e("startEvent", "is")
        + e("userTask", "innerUt")
        + error_end("iboom", "ERR_AAA")
        + f("if1", "is", "innerUt")
        + f("if2", "innerUt", "iboom")
        + event_subproc("escSub", esc_inner_sub)  # 只接 ERR_BBB，不匹配
    )
    esc_inner_top = (
        error_start("es-top-start", "ERR_AAA")
        + e("userTask", "esTopUt")
        + e("endEvent", "esTopEnd")
        + f("b1", "es-top-start", "esTopUt")
        + f("b2", "esTopUt", "esTopEnd")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + subproc("sub", sub_inner)
        + e("endEvent", "end")
        + f("f1", "start", "sub")
        + f("f2", "sub", "end")
        + event_subproc("escTop", esc_inner_top),  # 流程级，接 ERR_AAA
        "err-bubble",
    )
    pi = eng.start_process_instance_by_key("err-bubble")
    assert task_ids(eng, pi) == {"innerUt"}

    # 内部任务完成 -> ERR_AAA -> sub 级不匹配 -> 流程级事件子流程中断整个实例
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert task_ids(eng, pi) == {"esTopUt"}
    # sub scope 及其内部全被中断（含 innerUt 任务归档留痕）
    assert open_actinst_of(pi, "sub") == []
    assert {t.task_definition_key for t in pi.completed_tasks} == {"innerUt"}
    # sub scope 已 ENDED
    assert all(
        ex.state != ExecutionState.ACTIVE
        for ex in pi.executions.values()
        if ex.activity_id == "sub"
    )

    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


# ---------------------------------------------------------------------------
# 无捕获：等同 none end
# ---------------------------------------------------------------------------
def test_error_without_catcher_is_none_end():
    """error end 无任何事件子流程捕获 -> 等同 none end（当前路径正常结束）。"""
    eng = ProcessEngine()
    deploy(
        eng,
        e("startEvent", "start")
        + e("userTask", "ut")
        + error_end("boom", "ERR_NONE")
        + f("f1", "start", "ut")
        + f("f2", "ut", "boom"),
        "err-nocatch",
    )
    pi = eng.start_process_instance_by_key("err-nocatch")
    assert task_ids(eng, pi) == {"ut"}
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    # 等同普通 end：实例完成，错误被吞（对齐 Camunda 默认语义）
    assert pi.state == ProcessInstanceState.COMPLETED


# ---------------------------------------------------------------------------
# 事件子流程内部再抛错 + root 接管特判
# ---------------------------------------------------------------------------
def test_error_in_event_subprocess_bubbles_and_root_takeover():
    """root 到 error end -> es1 接管；es1 内部再抛 ERR_2 -> 冒泡被 es2 捕获。"""
    eng = ProcessEngine()
    calls = []

    def finalize(v):
        calls.append(1)
        return None

    eng.register_delegate("finalize", finalize)
    esc1_inner = (
        error_start("es1-start", "ERR_1")
        + e("userTask", "es1Ut")
        + error_end("es1boom", "ERR_2")
        + f("c1", "es1-start", "es1Ut")
        + f("c2", "es1Ut", "es1boom")
    )
    esc2_inner = (
        error_start("es2-start", "ERR_2")
        + svc("es2Svc", "finalize")
        + e("endEvent", "es2End")
        + f("d1", "es2-start", "es2Svc")
        + f("d2", "es2Svc", "es2End")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + e("userTask", "mainUt")
        + error_end("boom", "ERR_1")
        + f("f1", "start", "mainUt")
        + f("f2", "mainUt", "boom")
        + event_subproc("esc1", esc1_inner)
        + event_subproc("esc2", esc2_inner),
        "err-chain",
    )
    pi = eng.start_process_instance_by_key("err-chain")
    assert task_ids(eng, pi) == {"mainUt"}

    # 主线 complete -> root 到达 error end(ERR_1) -> esc1 接管（root 特判）
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert task_ids(eng, pi) == {"es1Ut"}
    assert pi.state == ProcessInstanceState.ACTIVE
    assert pi.root_execution.activity_id is None  # root 已清空作宿主

    # es1 任务完成 -> 事件子流程内部抛 ERR_2 -> 冒泡到流程级 esc2 接管
    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert calls == [1]  # es2 的 serviceTask 执行
    assert pi.state == ProcessInstanceState.COMPLETED
    # 中断的 es1 内部任务已归档留痕
    assert {t.task_definition_key for t in pi.completed_tasks} == {"mainUt", "es1Ut"}


# ---------------------------------------------------------------------------
# 中断范围：并行分支下的错误
# ---------------------------------------------------------------------------
def test_process_level_error_kills_parallel_siblings():
    """fork 分支之一抛错 -> 流程级事件子流程中断包括另一分支停等任务。"""
    eng = ProcessEngine()
    esc_inner = (
        error_start("es-start", "ERR_1")
        + e("userTask", "esUt")
        + e("endEvent", "esEnd")
        + f("ef1", "es-start", "esUt")
        + f("ef2", "esUt", "esEnd")
    )
    deploy(
        eng,
        e("startEvent", "start")
        + e("parallelGateway", "fork")
        + e("userTask", "branchOk")
        + e("userTask", "branchErr")
        + error_end("boom", "ERR_1")
        + e("endEvent", "end")
        + e("parallelGateway", "join")
        + f("f1", "start", "fork")
        + f("f2", "fork", "branchOk")
        + f("f3", "fork", "branchErr")
        + f("f4", "branchOk", "join")
        + f("f5", "branchErr", "boom")
        + f("f6", "boom", "join")
        + f("f7", "join", "end")
        + event_subproc("esc", esc_inner),
        "err-parallel",
    )
    pi = eng.start_process_instance_by_key("err-parallel")
    assert task_ids(eng, pi) == {"branchOk", "branchErr"}

    # branchErr 分支 complete -> error end -> 中断整个实例（branchOk 分支也被杀）
    err_task = next(
        t for t in eng.create_task_query(pi.id) if t.task_definition_key == "branchErr"
    )
    eng.complete_task(err_task.id)
    assert task_ids(eng, pi) == {"esUt"}
    assert {t.task_definition_key for t in pi.completed_tasks} == {"branchErr", "branchOk"}
    # 中断无残留：活跃执行只剩 root(宿主,activity 空) + 事件子流程 scope 及其内部 token
    actives = [ex for ex in pi.executions.values() if ex.state == ExecutionState.ACTIVE]
    assert all(
        ex.activity_id in (None, "esc", "esUt") for ex in actives
    )
    assert {ex.id for ex in actives} >= {pi.root_execution.id}

    eng.complete_task(eng.create_task_query(pi.id)[0].id)
    assert pi.state == ProcessInstanceState.COMPLETED


def test_subprocess_level_error_keeps_outer_parallel_branch():
    """sub 内抛错被 sub 级事件子流程吞掉 -> 外层并行兄弟分支不受影响。"""
    eng = ProcessEngine()
    esc_inner = (
        error_start("es-start", "ERR_1")
        + e("userTask", "esUt")
        + e("endEvent", "esEnd")
        + f("ef1", "es-start", "esUt")
        + f("ef2", "esUt", "esEnd")
    )
    sub_inner = (
        e("startEvent", "is")
        + e("userTask", "innerUt")
        + error_end("iboom", "ERR_1")
        + f("if1", "is", "innerUt")
        + f("if2", "innerUt", "iboom")
        + event_subproc("esc", esc_inner)
    )
    deploy(
        eng,
        e("startEvent", "start")
        + e("parallelGateway", "fork")
        + e("userTask", "outerUt")  # 外层并行分支（与 sub 并列）
        + subproc("sub", sub_inner)
        + e("endEvent", "end")
        + e("parallelGateway", "join")
        + f("f1", "start", "fork")
        + f("f2", "fork", "outerUt")
        + f("f3", "fork", "sub")
        + f("f4", "outerUt", "join")
        + f("f5", "sub", "join")
        + f("f6", "join", "end"),
        "err-par-sub",
    )
    pi = eng.start_process_instance_by_key("err-par-sub")
    assert task_ids(eng, pi) == {"outerUt", "innerUt"}

    # sub 内部任务完成 -> ERR_1 被 sub 内事件子流程捕获（sub 级中断）
    eng.complete_task(
        next(
            t for t in eng.create_task_query(pi.id) if t.task_definition_key == "innerUt"
        ).id
    )
    assert task_ids(eng, pi) == {"outerUt", "esUt"}  # 外层分支与事件子流程并存

    # 事件子流程收尾 -> sub 复活走 join -> 等 outerUt 完成后 join 汇聚
    eng.complete_task(
        next(
            t for t in eng.create_task_query(pi.id) if t.task_definition_key == "esUt"
        ).id
    )
    assert task_ids(eng, pi) == {"outerUt"}  # 主线已到 join 等待外层
    eng.complete_task(
        next(
            t for t in eng.create_task_query(pi.id) if t.task_definition_key == "outerUt"
        ).id
    )
    assert pi.state == ProcessInstanceState.COMPLETED
