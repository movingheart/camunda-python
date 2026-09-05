"""M4-2c2/2c3：多实例引擎语义测试（userTask / serviceTask / subProcess 宿主，内存模式）。

覆盖：
- parallel：N 实例任务并存（execution 树 = SCOPE 容器 + N child）、全部完成收束
- sequential：同一时刻仅 1 实例任务，逐个完成续跑
- collection + elementVariable / loopCardinality 两种实例集来源
- 空集合：零实例，宿主直接通过（无任务、无残留 MI 容器）
- completionCondition 提前终止：并行满足条件 -> 剩余实例任务取消归档、实例完成
- completionCondition 提前终止：顺序满足条件 -> 不再启动下一实例
- loopCounter / elementVariable 行为期注入、容器收尾清理
- 纯 MI 范围防御：宿主组合 asyncBefore / 边界事件 -> 运行时明确报错
- M4-2c3 serviceTask 宿主：同步 delegate 逐实例执行（并行/顺序）、条件提前终止
- M4-2c3 subProcess 宿主：实例各自进入内部流转停等、逐实例收束续跑/容器收束、
  条件满足终止剩余实例整树（任务归档、actinst 结算、无孤儿 execution）
"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import InvalidRequestException
from camunda.engine.process_engine import ProcessEngine
from camunda.model.execution import ExecutionState
from camunda.parser.bpmn_parser import parse_bpmn_xml

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
"""


def wrap(process_xml: str) -> str:
    return HEAD + process_xml + "\n</bpmn:definitions>\n"


def mi_user_task(
    node_id: str = "review",
    sequential: bool = False,
    collection: str | None = "${reviewers}",
    element_variable: str | None = "reviewer",
    cardinality: str | None = None,
    completion: str | None = None,
    extra_inner: str = "",
    extra_attrs: str = "",
) -> str:
    seq = ' isSequential="true"' if sequential else ""
    col_attr = f' camunda:collection="{collection}"' if collection else ""
    ev_attr = f' camunda:elementVariable="{element_variable}"' if element_variable else ""
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
        f'<bpmn:userTask id="{node_id}" name="会签"{extra_attrs}>'
        f'<bpmn:multiInstanceLoopCharacteristics{seq}{col_attr}{ev_attr}>'
        f"{card}{comp}{extra_inner}"
        f"</bpmn:multiInstanceLoopCharacteristics>"
        f"</bpmn:userTask>"
    )


def deploy_user_task_flow(
    engine: ProcessEngine,
    key: str,
    mi: str,
    after: str = '<bpmn:endEvent id="end"/>',
    flows_extra: str = '<bpmn:sequenceFlow id="f2" sourceRef="review" targetRef="end"/>',
) -> None:
    xml = wrap(
        f"""
  <bpmn:process id="{key}" name="MI-P" isExecutable="true">
    <bpmn:startEvent id="start"/>
    {mi}
    {after}
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="review"/>
    {flows_extra}
  </bpmn:process>
"""
    )
    assert key in engine.deploy(parse_bpmn_xml(xml, source_name=key))


def review_tasks(engine: ProcessEngine, pi_id: str) -> list:
    return [
        t
        for t in engine.create_task_query(process_instance_id=pi_id)
        if t.task_definition_key == "review"
    ]


def test_parallel_user_task_all_instances_wait():
    """并行：3 实例任务并存，全完成后实例收束。"""
    engine = ProcessEngine()
    deploy_user_task_flow(engine, "mi-par", mi_user_task())
    pi = engine.start_process_instance_by_key("mi-par", {"reviewers": ["a", "b", "c"]})
    tasks = review_tasks(engine, pi.id)
    assert len(tasks) == 3  # 三实例任务并存
    # execution 树：root(SCOPE@review 容器) + 3 条实例 child（各停 userTask）
    root = pi.root_execution
    assert root.role == "SCOPE" and root.activity_id == "review"
    assert root.is_mi_container and root.mi["total"] == 3
    assert len(root.children) == 3
    assert all(c.role == "TOKEN" and c.activity_id == "review" for c in root.children)
    assert all(c.mi == {"index": i} for i, c in enumerate(root.children))
    assert not pi.is_completed
    # 逐个完成：中间实例完成容器存活；末次实例完成容器收束离开
    for i, t in enumerate(tasks):
        assert not engine.get_process_instance(pi.id).is_completed
        engine.complete_task(t.id)
        if i < len(tasks) - 1:
            assert root.is_mi_container
            assert root.mi["completed"] == i + 1
        else:
            assert engine.get_process_instance(pi.id).is_completed
    assert engine.get_process_instance(pi.id).is_completed
    assert root.mi is None and root.role == "TOKEN"
    assert engine.create_task_query() == []
    # actinst：宿主 3 实例各留一条 userTask 痕迹 + 容器无独立痕迹
    acts = [a for a in pi.activity_history if a.activity_id == "review"]
    assert len(acts) == 3 and all(a.end_time is not None for a in acts)


def test_sequential_user_task_one_at_a_time():
    """顺序：同一时刻仅 1 实例任务，完成一个续跑下一个。"""
    engine = ProcessEngine()
    deploy_user_task_flow(engine, "mi-seq", mi_user_task(sequential=True))
    pi = engine.start_process_instance_by_key("mi-seq", {"reviewers": ["a", "b", "c"]})
    # 仅 1 个任务；token 自身即 MI 容器（不 SCOPE 化）
    root = pi.root_execution
    assert root.is_mi_container and root.role == "TOKEN"
    assert root.activity_id == "review"
    assert root.mi["sequential"] is True and root.mi["total"] == 3
    completed_seen = []
    for expect in range(3):
        (task,) = review_tasks(engine, pi.id)
        engine.complete_task(task.id)
        if expect < 2:
            # 中间实例完成：计数递增，续跑下一个（仍有 1 个待办）
            assert root.mi["completed"] == expect + 1
            assert len(review_tasks(engine, pi.id)) == 1
        else:
            # 末次实例完成：容器收束（mi=None、流程结束、无残留任务）
            assert engine.get_process_instance(pi.id).is_completed
            assert root.mi is None and engine.create_task_query() == []
    assert engine.get_process_instance(pi.id).is_completed
    assert root.mi is None and engine.create_task_query() == []


def test_collection_element_variable_injected_then_cleaned():
    """元素变量/loopCounter 行为期注入（并行最后一个实例值可见），收尾清理。"""
    engine = ProcessEngine()
    deploy_user_task_flow(engine, "mi-ev", mi_user_task())
    pi = engine.start_process_instance_by_key("mi-ev", {"reviewers": ["a", "b"]})
    # spawn 后变量表可见最后实例的注入值（行为期临时承载，文档化差异）
    assert pi.variables["loopCounter"] == 1
    assert pi.variables["reviewer"] == "b"
    for t in review_tasks(engine, pi.id):
        engine.complete_task(t.id)
    assert engine.get_process_instance(pi.id).is_completed
    assert "loopCounter" not in pi.variables
    assert "reviewer" not in pi.variables


def test_loop_cardinality_source():
    """loopCardinality 实例集来源：常量文本 3 -> 并行 3 实例。"""
    engine = ProcessEngine()
    deploy_user_task_flow(
        engine,
        "mi-card",
        mi_user_task(collection=None, element_variable=None, cardinality="3"),
    )
    pi = engine.start_process_instance_by_key("mi-card")
    assert len(review_tasks(engine, pi.id)) == 3
    for t in review_tasks(engine, pi.id):
        engine.complete_task(t.id)
    assert engine.get_process_instance(pi.id).is_completed


def test_empty_collection_passes_through():
    """空集合：零实例，宿主直接通过（无任务、无残留容器）。"""
    engine = ProcessEngine()
    deploy_user_task_flow(engine, "mi-empty", mi_user_task())
    pi = engine.start_process_instance_by_key("mi-empty", {"reviewers": []})
    assert engine.get_process_instance(pi.id).is_completed
    assert engine.create_task_query() == []
    assert pi.root_execution.mi is None


def test_parallel_completion_condition_kills_remaining():
    """并行完成条件提前满足：剩余实例任务被取消归档，活动结束。"""
    engine = ProcessEngine()
    deploy_user_task_flow(
        engine,
        "mi-cond",
        mi_user_task(completion="${nrOfCompletedInstances &gt;= 2}"),
    )
    pi = engine.start_process_instance_by_key("mi-cond", {"reviewers": ["a", "b", "c"]})
    tasks = review_tasks(engine, pi.id)
    assert len(tasks) == 3
    engine.complete_task(tasks[0].id)
    assert not engine.get_process_instance(pi.id).is_completed  # 1/3 条件未满足
    assert len(review_tasks(engine, pi.id)) == 2
    # 完成第 2 个：nrOfCompleted=2 条件满足 -> 第 3 实例被终止
    engine.complete_task(review_tasks(engine, pi.id)[0].id)
    assert engine.get_process_instance(pi.id).is_completed
    assert engine.create_task_query() == []  # 剩余任务已取消
    root = pi.root_execution
    assert root.mi is None and root.role == "TOKEN"
    # 被终止的第 3 个任务归档带 end_time（非泄漏）
    archived = {t.task_definition_key: t for t in pi.completed_tasks}
    assert sum(1 for t in pi.completed_tasks if t.task_definition_key == "review") == 3
    assert all(t.end_time is not None for t in pi.completed_tasks if t.task_definition_key == "review")


def test_sequential_completion_condition_stops():
    """顺序完成条件提前满足：不再启动下一实例。"""
    engine = ProcessEngine()
    deploy_user_task_flow(
        engine,
        "mi-seqcond",
        mi_user_task(sequential=True, completion="${nrOfCompletedInstances &gt;= 2}"),
    )
    pi = engine.start_process_instance_by_key("mi-seqcond", {"reviewers": ["a", "b", "c"]})
    (t1,) = review_tasks(engine, pi.id)
    engine.complete_task(t1.id)
    assert not engine.get_process_instance(pi.id).is_completed  # 1/3
    (t2,) = review_tasks(engine, pi.id)
    engine.complete_task(t2.id)
    # 2 满足条件 -> 不再产生第 3 个任务
    assert engine.get_process_instance(pi.id).is_completed
    assert engine.create_task_query() == []
    assert len([t for t in pi.completed_tasks if t.task_definition_key == "review"]) == 2


def test_mi_host_combination_rejected():
    """纯 MI 范围防御：宿主组合 asyncBefore / 边界事件 -> 运行时明确报错。"""
    # asyncBefore 组合
    engine = ProcessEngine()
    deploy_user_task_flow(
        engine, "mi-asy", mi_user_task(extra_attrs=' camunda:asyncBefore="true"')
    )
    with pytest.raises(InvalidRequestException, match="asyncBefore"):
        engine.start_process_instance_by_key("mi-asy", {"reviewers": ["a"]})
    # 边界事件组合：解析合法（边界挂 userTask），到达即报错
    engine2 = ProcessEngine()
    xml = wrap(
        """
  <bpmn:process id="mi-bnd" name="MI-B" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="review" name="会签">
      <bpmn:multiInstanceLoopCharacteristics isSequential="false"
          camunda:collection="${reviewers}"/>
    </bpmn:userTask>
    <bpmn:boundaryEvent id="esc" attachedToRef="review" cancelActivity="false">
      <bpmn:timerEventDefinition>
        <bpmn:timeDuration xsi:type="bpmn:tFormalExpression">PT5S</bpmn:timeDuration>
      </bpmn:timerEventDefinition>
    </bpmn:boundaryEvent>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="review"/>
    <bpmn:sequenceFlow id="f2" sourceRef="review" targetRef="end"/>
    <bpmn:sequenceFlow id="f3" sourceRef="esc" targetRef="end"/>
  </bpmn:process>
"""
    )
    assert "mi-bnd" in engine2.deploy(parse_bpmn_xml(xml, source_name="mi-bnd"))
    with pytest.raises(InvalidRequestException, match="边界事件"):
        engine2.start_process_instance_by_key("mi-bnd", {"reviewers": ["a"]})


# ---------------------------------------------------------------------------
# M4-2c3：serviceTask / subProcess 宿主多实例
# ---------------------------------------------------------------------------
def mi_service_task(
    node_id: str = "proc",
    impl: str = "work",
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
        f'<bpmn:serviceTask id="{node_id}" camunda:delegateExpression="${{{impl}}}">'
        f'<bpmn:multiInstanceLoopCharacteristics{seq}{col_attr}{ev_attr}>'
        f"{card}{comp}"
        f"</bpmn:multiInstanceLoopCharacteristics>"
        f"</bpmn:serviceTask>"
    )


def mi_subprocess(
    node_id: str,
    inner: str,
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
        f'<bpmn:subProcess id="{node_id}">'
        f'<bpmn:multiInstanceLoopCharacteristics{seq}{col_attr}{ev_attr}>'
        f"{card}{comp}"
        f"</bpmn:multiInstanceLoopCharacteristics>"
        f"{inner}"
        f"</bpmn:subProcess>"
    )


def deploy_mi_flow(
    engine: ProcessEngine, key: str, mi: str, node_id: str = "review"
) -> None:
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


def inner_wait_flow() -> str:
    """subProcess 内部：start -> userTask 停等 -> end。"""
    return (
        '<bpmn:startEvent id="is"/>'
        '<bpmn:userTask id="innerTask" name="内审"/>'
        '<bpmn:endEvent id="ie"/>'
        '<bpmn:sequenceFlow id="if1" sourceRef="is" targetRef="innerTask"/>'
        '<bpmn:sequenceFlow id="if2" sourceRef="innerTask" targetRef="ie"/>'
    )


def test_parallel_service_task_sync_instances():
    """并行 serviceTask：同步 delegate 逐实例执行，元素变量按序可见后清理。"""
    engine = ProcessEngine()
    seen = []

    def work(v):
        seen.append((v["loopCounter"], v["reviewer"]))
        return None

    engine.register_delegate("work", work)
    deploy_mi_flow(engine, "mi-svc-par", mi_service_task(), "proc")
    pi = engine.start_process_instance_by_key("mi-svc-par", {"reviewers": ["a", "b", "c"]})
    # 同步宿主：start 返回即已全部执行完并收束
    assert engine.get_process_instance(pi.id).is_completed
    assert seen == [(0, "a"), (1, "b"), (2, "c")]
    root = pi.root_execution
    assert root.mi is None and root.role == "TOKEN"
    assert [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE] == []
    assert engine.create_task_query() == []
    # 每实例一条 serviceTask actinst（已结算），容器无独立痕迹
    acts = [a for a in pi.activity_history if a.activity_id == "proc"]
    assert len(acts) == 3 and all(a.end_time is not None for a in acts)
    assert "loopCounter" not in pi.variables
    assert "reviewer" not in pi.variables


def test_sequential_service_task_in_order():
    """顺序 serviceTask：同一容器 token 按 loopCounter 顺序同步执行（loopCardinality 源）。"""
    engine = ProcessEngine()
    seen = []
    engine.register_delegate("work", lambda v: seen.append(v["loopCounter"]) or None)
    deploy_mi_flow(
        engine,
        "mi-svc-seq",
        mi_service_task(
            sequential=True,
            collection=None,
            element_variable=None,
            cardinality="3",
        ),
        "proc",
    )
    pi = engine.start_process_instance_by_key("mi-svc-seq")
    assert engine.get_process_instance(pi.id).is_completed
    assert seen == [0, 1, 2]
    acts = [a for a in pi.activity_history if a.activity_id == "proc"]
    assert len(acts) == 3 and all(a.end_time is not None for a in acts)
    assert engine.create_task_query() == []
    assert pi.root_execution.mi is None and pi.root_execution.role == "TOKEN"


def test_service_task_completion_condition_early_stop():
    """并行 serviceTask completionCondition：满足即终止，剩余未启动实例不再生成。"""
    engine = ProcessEngine()
    calls = []
    engine.register_delegate("work", lambda v: calls.append(1) or None)
    deploy_mi_flow(
        engine,
        "mi-svc-cond",
        mi_service_task(completion="${nrOfCompletedInstances &gt;= 2}"),
        "proc",
    )
    pi = engine.start_process_instance_by_key("mi-svc-cond", {"reviewers": ["a", "b", "c"]})
    assert engine.get_process_instance(pi.id).is_completed
    assert len(calls) == 2  # 第 3 实例未启动（容器收束即止）
    acts = [a for a in pi.activity_history if a.activity_id == "proc"]
    assert len(acts) == 2 and all(a.end_time is not None for a in acts)
    assert pi.root_execution.mi is None
    assert [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE] == []


def test_parallel_subprocess_host_instances_wait():
    """并行 subProcess 宿主：实例各自进内部流转停等；全收束容器离开。"""
    engine = ProcessEngine()
    deploy_mi_flow(
        engine, "mi-sub-par", mi_subprocess("batch", inner_wait_flow()), "batch"
    )
    pi = engine.start_process_instance_by_key("mi-sub-par", {"reviewers": ["a", "b"]})
    assert not engine.get_process_instance(pi.id).is_completed
    root = pi.root_execution
    assert root.role == "SCOPE" and root.activity_id == "batch"
    assert root.is_mi_container and root.mi["total"] == 2 and root.mi["active"] == 2
    assert len(root.children) == 2
    insts = sorted(root.children, key=lambda c: c.mi["index"])
    for i, inst in enumerate(insts):
        assert inst.role == "SCOPE" and inst.activity_id == "batch"
        assert inst.mi == {"index": i}
        # 实例 scope 下挂 1 条内部 token 停等 innerTask（内部 token 无 mi）
        (tok,) = inst.children
        assert tok.activity_id == "innerTask" and tok.mi is None
    tasks = engine.create_task_query(process_instance_id=pi.id)
    assert len(tasks) == 2 and all(t.task_definition_key == "innerTask" for t in tasks)
    # 行为期承载最后注入的实例变量（并行实例集共享实例级变量，文档化差异）
    assert pi.variables["loopCounter"] == 1 and pi.variables["reviewer"] == "b"
    # 完成第 1 条实例：实例 scope 收束摘树，容器存活、计数回落
    engine.complete_task(tasks[0].id)
    assert not engine.get_process_instance(pi.id).is_completed
    assert len(root.children) == 1
    assert root.mi["completed"] == 1 and root.mi["active"] == 1
    assert len(engine.create_task_query(process_instance_id=pi.id)) == 1
    # 完成最后一条实例：容器收束离开 -> 流程结束、无残留
    engine.complete_task(engine.create_task_query(process_instance_id=pi.id)[0].id)
    assert engine.get_process_instance(pi.id).is_completed
    assert root.mi is None and root.role == "TOKEN"
    assert engine.create_task_query() == []
    assert [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE] == []
    assert "loopCounter" not in pi.variables and "reviewer" not in pi.variables
    # 每实例各留一条 batch + innerTask actinst（均结算）
    batch_acts = [a for a in pi.activity_history if a.activity_id == "batch"]
    assert len(batch_acts) == 2 and all(a.end_time is not None for a in batch_acts)
    inner_acts = [a for a in pi.activity_history if a.activity_id == "innerTask"]
    assert len(inner_acts) == 2 and all(a.end_time is not None for a in inner_acts)


def test_sequential_subprocess_host_one_at_a_time():
    """顺序 subProcess 宿主：单实例进内部流转，完成一个续跑下一个。"""
    engine = ProcessEngine()
    deploy_mi_flow(
        engine,
        "mi-sub-seq",
        mi_subprocess("batch", inner_wait_flow(), sequential=True),
        "batch",
    )
    pi = engine.start_process_instance_by_key("mi-sub-seq", {"reviewers": ["a", "b", "c"]})
    root = pi.root_execution
    assert not engine.get_process_instance(pi.id).is_completed
    # 容器 token 兼实例载体：已进 subProcess（SCOPE@batch），仅 1 条内部任务
    assert root.is_mi_container and root.mi["sequential"] is True and root.mi["total"] == 3
    assert root.role == "SCOPE" and root.activity_id == "batch"
    assert len(root.children) == 1
    assert pi.variables["reviewer"] == "a"  # 当前实例元素变量可见
    for expect in range(3):
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        engine.complete_task(task.id)
        if expect < 2:
            # 实例完成 -> 下一实例续跑（仍 1 条内部任务、元素变量前移）
            assert root.is_mi_container and root.mi["completed"] == expect + 1
            assert len(root.children) == 1
            assert len(engine.create_task_query(process_instance_id=pi.id)) == 1
            assert pi.variables["reviewer"] == "abc"[expect + 1]
        else:
            assert engine.get_process_instance(pi.id).is_completed
            assert root.mi is None and root.role == "TOKEN"
    assert engine.create_task_query() == []
    assert [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE] == []
    batch_acts = [a for a in pi.activity_history if a.activity_id == "batch"]
    assert len(batch_acts) == 3 and all(a.end_time is not None for a in batch_acts)
    assert "reviewer" not in pi.variables and "loopCounter" not in pi.variables


def test_parallel_subprocess_completion_condition_kills_instances():
    """并行 subProcess 宿主 completionCondition：满足后整树终止剩余实例。"""
    engine = ProcessEngine()
    deploy_mi_flow(
        engine,
        "mi-sub-cond",
        mi_subprocess(
            "batch", inner_wait_flow(), completion="${nrOfCompletedInstances &gt;= 2}"
        ),
        "batch",
    )
    pi = engine.start_process_instance_by_key("mi-sub-cond", {"reviewers": ["a", "b", "c"]})
    root = pi.root_execution
    assert len(engine.create_task_query(process_instance_id=pi.id)) == 3
    # 完成两条实例 -> 条件满足，第 3 条实例（含内部停等）被整树终止
    engine.complete_task(engine.create_task_query(process_instance_id=pi.id)[0].id)
    assert not engine.get_process_instance(pi.id).is_completed
    engine.complete_task(engine.create_task_query(process_instance_id=pi.id)[0].id)
    assert engine.get_process_instance(pi.id).is_completed
    assert engine.create_task_query() == []
    assert root.mi is None and root.role == "TOKEN"
    assert [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE] == []
    # 3 条内部任务全部归档（2 完成 + 1 被终止），均带 end_time
    archived = [t for t in pi.completed_tasks if t.task_definition_key == "innerTask"]
    assert len(archived) == 3 and all(t.end_time is not None for t in archived)
    batch_acts = [a for a in pi.activity_history if a.activity_id == "batch"]
    assert len(batch_acts) == 3 and all(a.end_time is not None for a in batch_acts)


def test_subprocess_host_empty_collection_passes_through():
    """subProcess 宿主空集合：零实例直接通过，无容器/无内部残留。"""
    engine = ProcessEngine()
    deploy_mi_flow(
        engine, "mi-sub-empty", mi_subprocess("batch", inner_wait_flow()), "batch"
    )
    pi = engine.start_process_instance_by_key("mi-sub-empty", {"reviewers": []})
    assert engine.get_process_instance(pi.id).is_completed
    assert engine.create_task_query() == []
    assert pi.root_execution.mi is None
    assert [e for e in pi.executions.values() if e.state == ExecutionState.ACTIVE] == []
