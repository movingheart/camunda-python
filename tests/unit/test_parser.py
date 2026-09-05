"""BPMN 解析器单元测试。"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import DeploymentException
from camunda.model.bpmn import (
    EndEvent,
    ExclusiveGateway,
    ParallelGateway,
    ServiceTask,
    StartEvent,
    UserTask,
)
from camunda.parser import parse_bpmn_file, parse_bpmn_xml

SIMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://example">
  <bpmn:process id="simple" name="简单流程" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:userTask id="task" name="审批"/>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="task"/>
    <bpmn:sequenceFlow id="f2" sourceRef="task" targetRef="end"/>
  </bpmn:process>
</bpmn:definitions>
"""


def test_parse_simple_process():
    model = parse_bpmn_xml(SIMPLE_XML)
    assert model.process_keys() == ["simple"]
    proc = model.get_process("simple")
    assert proc.name == "简单流程"
    assert isinstance(proc.flow_nodes["start"], StartEvent)
    assert isinstance(proc.flow_nodes["task"], UserTask)
    assert proc.flow_nodes["task"].name == "审批"
    assert isinstance(proc.flow_nodes["end"], EndEvent)


def test_flow_wiring_and_order():
    model = parse_bpmn_xml(SIMPLE_XML)
    proc = model.get_process("simple")
    task = proc.flow_nodes["task"]
    # 入边 f1、出边 f2 已回填
    assert task.incoming == ["f1"]
    assert task.outgoing == ["f2"]
    # 连线引用校验
    assert proc.sequence_flows["f1"].source_ref == "start"
    assert proc.sequence_flows["f1"].target_ref == "task"


def test_parse_loan_example():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "examples" / "loan-approval.bpmn"
    model = parse_bpmn_file(str(path))
    proc = model.get_process("loan-approval")
    assert isinstance(proc.flow_nodes["amount-gateway"], ExclusiveGateway)
    assert proc.flow_nodes["amount-gateway"].default_flow == "flow-auto-pass"
    assert isinstance(proc.flow_nodes["check-credit"], ServiceTask)
    # delegateExpression="${checkCredit}" -> 注册名 checkCredit
    assert proc.flow_nodes["check-credit"].implementation_ref == "checkCredit"
    # 条件流解析
    flow = proc.sequence_flows["flow-manual"]
    assert flow.condition_expression == "${amount >= 10000}"
    assert proc.sequence_flows["flow-auto-pass"].condition_expression is None


def test_parse_parallel_example():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "examples" / "parallel-review.bpmn"
    model = parse_bpmn_file(str(path))
    proc = model.get_process("parallel-review")
    assert isinstance(proc.flow_nodes["fork"], ParallelGateway)
    # fork: 2 出边；join: 2 入边
    assert proc.flow_nodes["fork"].outgoing == ["f2", "f3"]
    assert proc.flow_nodes["join"].incoming == ["f4", "f5"]


def test_missing_start_event_raises():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <bpmn:process id="p" isExecutable="true">
    <bpmn:userTask id="t"/>
  </bpmn:process>
</bpmn:definitions>
"""
    with pytest.raises(DeploymentException, match="startEvent"):
        parse_bpmn_xml(xml)


def test_bad_xml_raises():
    with pytest.raises(DeploymentException, match="XML"):
        parse_bpmn_xml("<bpmn:definitions><broken")


def test_dangling_flow_reference_raises():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <bpmn:process id="p" isExecutable="true">
    <bpmn:startEvent id="s"/>
    <bpmn:endEvent id="e"/>
    <bpmn:sequenceFlow id="f" sourceRef="s" targetRef="ghost"/>
  </bpmn:process>
</bpmn:definitions>
"""
    with pytest.raises(DeploymentException, match="ghost"):
        parse_bpmn_xml(xml)
