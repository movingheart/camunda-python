"""M5-4：businessRuleTask 引擎集成测试（BPMN -> DMN 决策 -> 变量合并）。"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import DeploymentException, NotFoundException
from camunda.engine.process_engine import ProcessEngine
from camunda.parser import parse_bpmn_xml
from camunda.parser.dmn_parser import parse_dmn_xml

DMN = """<?xml version="1.0" encoding="UTF-8"?>
<dmn:definitions xmlns:dmn="https://www.omg.org/spec/DMN/20191111/MODEL/">
  <dmn:decision id="loan-grade">
    <dmn:decisionTable hitPolicy="UNIQUE">
      <dmn:input id="i1"><dmn:inputExpression id="ie1">amount</dmn:inputExpression></dmn:input>
      <dmn:output id="o1" name="grade"/>
      <dmn:rule id="r1"><dmn:inputEntry><dmn:text>&lt;= 5000</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>"A"</dmn:text></dmn:outputEntry></dmn:rule>
      <dmn:rule id="r2"><dmn:inputEntry><dmn:text>&gt; 5000</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>"B"</dmn:text></dmn:outputEntry></dmn:rule>
    </dmn:decisionTable>
  </dmn:decision>
  <dmn:decision id="risk-rows">
    <dmn:decisionTable hitPolicy="RULE ORDER">
      <dmn:input id="i1"><dmn:inputExpression id="ie1">grade</dmn:inputExpression></dmn:input>
      <dmn:output id="o1" name="risk"/>
      <dmn:rule id="r1"><dmn:inputEntry><dmn:text>"A"</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>"low"</dmn:text></dmn:outputEntry></dmn:rule>
      <dmn:rule id="r2"><dmn:inputEntry><dmn:text>"B"</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>"high"</dmn:text></dmn:outputEntry></dmn:rule>
    </dmn:decisionTable>
  </dmn:decision>
</dmn:definitions>
"""

BPMN_TPL = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn">
  <bpmn:process id="loan-flow" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:businessRuleTask id="grade" camunda:decisionRef="loan-grade" camunda:resultVariable="grade"/>
    <bpmn:businessRuleTask id="risk" camunda:decisionRef="risk-rows"{extra}/>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="grade"/>
    <bpmn:sequenceFlow id="f2" sourceRef="grade" targetRef="risk"/>
    <bpmn:sequenceFlow id="f3" sourceRef="risk" targetRef="end"/>
  </bpmn:process>
</bpmn:definitions>
"""


def make_engine(bpmn_xml: str | None = None) -> ProcessEngine:
    if bpmn_xml is None:
        bpmn_xml = BPMN_TPL.replace("{extra}", "")
    engine = ProcessEngine()
    engine.deploy_dmn(parse_dmn_xml(DMN))
    engine.deploy(parse_bpmn_xml(bpmn_xml))
    return engine


def test_business_rule_task_evaluates_and_merges_result():
    engine = make_engine()
    pi = engine.start_process_instance_by_key("loan-flow", {"amount": 3000})
    assert pi.is_completed
    # 第一个决策 -> grade 变量（标量）；其值再喂给第二个决策（RULE ORDER -> 列表）
    assert pi.variables["grade"] == "A"
    assert pi.variables["result"] == ["low"]


def test_business_rule_task_custom_result_variable():
    bpmn = BPMN_TPL.replace(
        'camunda:decisionRef="risk-rows"',
        'camunda:decisionRef="risk-rows" camunda:resultVariable="myRisk"',
    ).replace("{extra}", "")
    engine = make_engine(bpmn)
    pi = engine.start_process_instance_by_key("loan-flow", {"amount": 9000})
    assert pi.is_completed
    assert pi.variables["grade"] == "B"
    assert pi.variables["myRisk"] == ["high"]
    assert "result" not in pi.variables


def test_business_rule_task_missing_decision_ref_rejected():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn">
  <bpmn:process id="p" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:businessRuleTask id="brt"/>
    <bpmn:endEvent id="end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="brt"/>
    <bpmn:sequenceFlow id="f2" sourceRef="brt" targetRef="end"/>
  </bpmn:process>
</bpmn:definitions>
"""
    with pytest.raises(DeploymentException, match="decisionRef"):
        ProcessEngine().deploy(parse_bpmn_xml(xml))


def test_business_rule_task_undeployed_decision_runtime_error():
    engine = ProcessEngine()
    engine.deploy(parse_bpmn_xml(BPMN_TPL.replace("{extra}", "")))
    with pytest.raises(NotFoundException, match="未部署的决策定义"):
        engine.start_process_instance_by_key("loan-flow", {"amount": 1})


def test_decision_result_drives_exclusive_gateway():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn">
  <bpmn:process id="gw-flow" isExecutable="true">
    <bpmn:startEvent id="start"/>
    <bpmn:businessRuleTask id="brt" camunda:decisionRef="loan-grade"/>
    <bpmn:exclusiveGateway id="xor" default="f-else"/>
    <bpmn:endEvent id="fast"/>
    <bpmn:endEvent id="else-end"/>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="brt"/>
    <bpmn:sequenceFlow id="f2" sourceRef="brt" targetRef="xor"/>
    <bpmn:sequenceFlow id="f-fast" sourceRef="xor" targetRef="fast">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">${result == "A"}</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="f-else" sourceRef="xor" targetRef="else-end"/>
  </bpmn:process>
</bpmn:definitions>
"""
    engine = ProcessEngine()
    engine.deploy_dmn(parse_dmn_xml(DMN))
    engine.deploy(parse_bpmn_xml(xml))
    pi = engine.start_process_instance_by_key("gw-flow", {"amount": 1000})
    assert pi.is_completed
    # 决策结果 A -> 走 fast 路径（分支到达 end 顺序无痕，用变量区分不了，验证完成即可）
    assert pi.variables["result"] == "A"


def test_dmn_deploy_not_persisted_documented():
    """文档化差异：DMN 部署不落库，重启后须重新 deploy_dmn（对齐 delegate）。"""
    import tempfile
    import os

    from camunda.persistence.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "c.db")
        e1 = make_engine()
        e1._store = Store(db)
        # 重启后（DMN 注册表清空）：求值报未部署
        e2 = ProcessEngine.from_database(db)
        e2.deploy(parse_bpmn_xml(BPMN_TPL.replace("{extra}", "")))
        with pytest.raises(NotFoundException):
            e2.evaluate_decision("loan-grade", {"amount": 1})
        # 重新部署后恢复
        e2.deploy_dmn(parse_dmn_xml(DMN))
        assert e2.evaluate_decision("loan-grade", {"amount": 1}) == "A"
