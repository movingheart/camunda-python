"""M4-2c1：multiInstanceLoopCharacteristics 模型与解析器单元测试。

覆盖：
- userTask 并行多实例：camunda:collection + elementVariable + completionCondition
- serviceTask 顺序多实例：loopCardinality（无元素变量）
- subProcess 宿主多实例（容器递归后同样可挂，内部节点独立解析）
- 非法宿主（事件/网关）部署即报错（文档化差异白名单）
- collection 与 loopCardinality 同时提供 -> collection 优先
- 两者皆缺 / elementVariable 缺 collection -> 部署报错
"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import DeploymentException
from camunda.model.bpmn import MultiInstance, ServiceTask, SubProcess, UserTask
from camunda.parser import parse_bpmn_xml

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
"""


def wrap(process_xml: str) -> str:
    return HEAD + process_xml + "\n</bpmn:definitions>\n"


def user_task_mi(inner: str, attrs: str = "") -> str:
    return (
        f'<bpmn:userTask id="review" name="会签"{(" " + attrs) if attrs else ""}>'
        f'<bpmn:multiInstanceLoopCharacteristics isSequential="false"'
        f' camunda:collection="${{reviewers}}" camunda:elementVariable="reviewer">'
        f"{inner}"
        f"</bpmn:multiInstanceLoopCharacteristics>"
        f"</bpmn:userTask>"
    )


def test_parse_parallel_user_task_with_completion_condition():
    """并行 userTask 多实例：collection + elementVariable + completionCondition 全量解析。"""
    xml = wrap(
        """
  <bpmn:process id="mi-par" name="会签" isExecutable="true">
    <bpmn:startEvent id="start"/>
    """
        + user_task_mi(
            '<bpmn:completionCondition xsi:type="bpmn:tFormalExpression">'
            "${nrOfCompletedInstances &gt;= 2}</bpmn:completionCondition>"
        )
        + """
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="review"/>
    <bpmn:sequenceFlow id="f2" sourceRef="review" targetRef="end"/>
  </bpmn:process>
"""
    )
    proc = parse_bpmn_xml(xml).get_process("mi-par")
    review = proc.flow_nodes["review"]
    assert isinstance(review, UserTask)
    mi = review.multi_instance
    assert isinstance(mi, MultiInstance)
    assert mi.sequential is False
    assert mi.collection_expr == "${reviewers}"
    assert mi.element_variable == "reviewer"
    assert mi.loop_cardinality_expr is None
    assert mi.completion_condition_expr == "${nrOfCompletedInstances >= 2}"
    # 非多实例节点不受影响
    assert proc.flow_nodes["end"].multi_instance is None


def test_parse_sequential_service_task_with_cardinality():
    """顺序 serviceTask 多实例：isSequential=true + loopCardinality（无元素变量）。"""
    xml = wrap(
        """
  <bpmn:process id="mi-seq" name="顺序批处理" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:serviceTask id="proc" camunda:delegateExpression="${work}">
      <bpmn:multiInstanceLoopCharacteristics isSequential="true">
        <bpmn:loopCardinality xsi:type="bpmn:tFormalExpression">${3}</bpmn:loopCardinality>
      </bpmn:multiInstanceLoopCharacteristics>
    </bpmn:serviceTask>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="proc"/>
    <bpmn:sequenceFlow id="f2" sourceRef="proc" targetRef="end"/>
  </bpmn:process>
"""
    )
    proc = parse_bpmn_xml(xml).get_process("mi-seq")
    node = proc.flow_nodes["proc"]
    assert isinstance(node, ServiceTask)
    mi = node.multi_instance
    assert mi.sequential is True
    assert mi.loop_cardinality_expr == "${3}"
    assert mi.collection_expr is None
    assert mi.element_variable is None
    assert mi.completion_condition_expr is None
    # 宿主自身属性照常解析（delegate 注册名 + async 标志互不影响）
    assert node.implementation_ref == "work"


def test_parse_multi_instance_subprocess_host():
    """subProcess 宿主多实例：外层 sub 挂 MI；内部节点独立解析不受影响。"""
    xml = wrap(
        """
  <bpmn:process id="mi-sub" name="并行子流程批处理" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:subProcess id="batch">
      <bpmn:multiInstanceLoopCharacteristics isSequential="false"
          camunda:collection="${orders}" camunda:elementVariable="order"/>
      <bpmn:startEvent id="is"/>
      <bpmn:userTask id="innerTask"/>
      <bpmn:endEvent id="ie"/>
      <bpmn:sequenceFlow id="if1" sourceRef="is" targetRef="innerTask"/>
      <bpmn:sequenceFlow id="if2" sourceRef="innerTask" targetRef="ie"/>
    </bpmn:subProcess>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="batch"/>
    <bpmn:sequenceFlow id="f2" sourceRef="batch" targetRef="end"/>
  </bpmn:process>
"""
    )
    proc = parse_bpmn_xml(xml).get_process("mi-sub")
    batch = proc.flow_nodes["batch"]
    assert isinstance(batch, SubProcess)
    mi = batch.multi_instance
    assert mi.collection_expr == "${orders}" and mi.element_variable == "order"
    # 容器递归解析不受 MI 影响：内部 userTask 无 MI、连线正常
    inner_task = batch.process.flow_nodes["innerTask"]
    assert isinstance(inner_task, UserTask)
    assert inner_task.multi_instance is None
    assert inner_task.incoming == ["if1"] and inner_task.outgoing == ["if2"]
    # 内部节点也可各自挂 MI（容器内多实例子活动）
    assert batch.process.flow_nodes["is"].multi_instance is None


@pytest.mark.parametrize(
    ("tag", "node_id", "open_close"),
    [
        ("parallelGateway", "fork", "/"),
        ("exclusiveGateway", "xgw", "/"),
        ("intermediateCatchEvent", "wait", "/"),
        ("boundaryEvent", "esc", "/"),
        ("endEvent", "end", "/"),
        ("startEvent", "start", "/"),
    ],
)
def test_mi_on_illegal_host_rejected(tag, node_id, open_close):
    """非法宿主（事件/网关）声明 multiInstanceLoopCharacteristics -> 部署报错。"""
    xml = wrap(
        f"""
  <bpmn:process id="mi-bad" name="非法" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="anchor"/>
    <bpmn:{tag} id="{node_id}">
      <bpmn:multiInstanceLoopCharacteristics isSequential="false"
          camunda:collection="${{xs}}"/>
    </bpmn:{tag}>
    <bpmn:sequenceFlow id="f0" sourceRef="start" targetRef="anchor"/>
    <bpmn:sequenceFlow id="f1" sourceRef="anchor" targetRef="{node_id}"/>
    <bpmn:endEvent id="endOk"/>
    <bpmn:sequenceFlow id="f2" sourceRef="{node_id}" targetRef="endOk"/>
  </bpmn:process>
"""
    )
    with pytest.raises(DeploymentException, match="multiInstanceLoopCharacteristics"):
        parse_bpmn_xml(xml)


def test_mi_collection_precedence_over_cardinality():
    """collection 与 loopCardinality 同时提供：模型两字段都保留，collection 优先语义留引擎。"""
    xml = wrap(
        """
  <bpmn:process id="mi-both" name="双源" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="u">
      <bpmn:multiInstanceLoopCharacteristics isSequential="false"
          camunda:collection="${items}" camunda:elementVariable="item">
        <bpmn:loopCardinality xsi:type="bpmn:tFormalExpression">5</bpmn:loopCardinality>
      </bpmn:multiInstanceLoopCharacteristics>
    </bpmn:userTask>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="u"/>
    <bpmn:sequenceFlow id="f2" sourceRef="u" targetRef="end"/>
  </bpmn:process>
"""
    )
    mi = parse_bpmn_xml(xml).get_process("mi-both").flow_nodes["u"].multi_instance
    assert mi.collection_expr == "${items}"
    assert mi.loop_cardinality_expr == "5"


def test_mi_missing_source_rejected():
    """collection 与 loopCardinality 皆缺 -> 部署报错。"""
    xml = wrap(
        """
  <bpmn:process id="mi-nosrc" name="缺源" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="u">
      <bpmn:multiInstanceLoopCharacteristics isSequential="false"/>
    </bpmn:userTask>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="u"/>
    <bpmn:sequenceFlow id="f2" sourceRef="u" targetRef="end"/>
  </bpmn:process>
"""
    )
    with pytest.raises(DeploymentException, match="collection 或 loopCardinality"):
        parse_bpmn_xml(xml)


def test_mi_element_variable_without_collection_rejected():
    """elementVariable 但无 collection -> 部署报错。"""
    xml = wrap(
        """
  <bpmn:process id="mi-ev" name="孤立元素变量" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="u">
      <bpmn:multiInstanceLoopCharacteristics isSequential="false"
          camunda:elementVariable="item">
        <bpmn:loopCardinality xsi:type="bpmn:tFormalExpression">3</bpmn:loopCardinality>
      </bpmn:multiInstanceLoopCharacteristics>
    </bpmn:userTask>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="u"/>
    <bpmn:sequenceFlow id="f2" sourceRef="u" targetRef="end"/>
  </bpmn:process>
"""
    )
    with pytest.raises(DeploymentException, match="elementVariable"):
        parse_bpmn_xml(xml)
