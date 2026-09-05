"""M5-1：DMN 解析器测试（decisionTable 结构 + 部署校验）。"""

from __future__ import annotations

import pytest

from camunda.common.exceptions import DeploymentException
from camunda.parser.dmn_parser import parse_dmn_xml

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<dmn:definitions xmlns:dmn="https://www.omg.org/spec/DMN/20191111/MODEL/">
"""
TAIL = "</dmn:definitions>\n"


def wrap(body: str) -> str:
    return HEAD + body + TAIL


def simple_table(hit_policy: str = "UNIQUE", extra_rule: str = "") -> str:
    return wrap(
        f"""
  <dmn:decision id="d1" name="决策一">
    <dmn:decisionTable hitPolicy="{hit_policy}">
      <dmn:input id="i1" label="Amount">
        <dmn:inputExpression id="ie1" typeRef="number">amount</dmn:inputExpression>
      </dmn:input>
      <dmn:output id="o1" name="grade" typeRef="string"/>
      <dmn:rule id="r1">
        <dmn:inputEntry><dmn:text>&lt;= 100</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>"low"</dmn:text></dmn:outputEntry>
      </dmn:rule>
      {extra_rule}
    </dmn:decisionTable>
  </dmn:decision>
"""
    )


def test_parse_basic_decision_table():
    model = parse_dmn_xml(simple_table())
    assert model.decision_keys() == ["d1"]
    dec = model.get_decision("d1")
    assert dec.name == "决策一"
    t = dec.decision_table
    assert t.hit_policy == "UNIQUE"
    assert t.inputs[0].expression == "amount"
    assert t.inputs[0].name == "Amount"
    assert t.outputs[0].result_key() == "grade"
    assert t.rules[0].input_entries == ["<= 100"]
    assert t.rules[0].output_entries == ['"low"']


def test_parse_wildcard_and_dash_entries():
    xml = wrap(
        """
  <dmn:decision id="d1">
    <dmn:decisionTable hitPolicy="FIRST">
      <dmn:input id="i1"><dmn:inputExpression>amount</dmn:inputExpression></dmn:input>
      <dmn:output id="o1" name="grade"/>
      <dmn:rule id="r1">
        <dmn:inputEntry><dmn:text></dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>-</dmn:text></dmn:outputEntry>
      </dmn:rule>
    </dmn:decisionTable>
  </dmn:decision>
"""
    )
    rule = parse_dmn_xml(xml).get_decision("d1").decision_table.rules[0]
    assert rule.input_entries == [None]  # 空文本 -> 通配
    assert rule.output_entries == [None]  # "-" -> 空输出


def test_parse_output_values_priority_list():
    xml = wrap(
        """
  <dmn:decision id="d1">
    <dmn:decisionTable hitPolicy="PRIORITY">
      <dmn:input id="i1"><dmn:inputExpression>a</dmn:inputExpression></dmn:input>
      <dmn:output id="o1" name="g">
        <dmn:outputValues><dmn:text>"gold","silver", "bronze"</dmn:text></dmn:outputValues>
      </dmn:output>
      <dmn:rule id="r1">
        <dmn:inputEntry><dmn:text>true</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>"bronze"</dmn:text></dmn:outputEntry>
      </dmn:rule>
    </dmn:decisionTable>
  </dmn:decision>
"""
    )
    out = parse_dmn_xml(xml).get_decision("d1").decision_table.outputs[0]
    assert out.output_values == ['"gold"', '"silver"', '"bronze"']


def test_missing_decision_table_rejected():
    xml = wrap(
        """
  <dmn:decision id="d1">
    <dmn:literalExpression>
      <dmn:text>1 + 1</dmn:text>
    </dmn:literalExpression>
  </dmn:decision>
"""
    )
    with pytest.raises(DeploymentException, match="仅支持 decisionTable"):
        parse_dmn_xml(xml)


def test_unknown_hit_policy_rejected():
    with pytest.raises(DeploymentException, match="未知 hitPolicy"):
        parse_dmn_xml(simple_table(hit_policy="SUPER"))


def test_aggregation_requires_collect():
    xml = wrap(
        """
  <dmn:decision id="d1">
    <dmn:decisionTable hitPolicy="FIRST" aggregation="SUM">
      <dmn:input id="i1"><dmn:inputExpression>a</dmn:inputExpression></dmn:input>
      <dmn:output id="o1" name="g"/>
      <dmn:rule id="r1">
        <dmn:inputEntry><dmn:text>true</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>1</dmn:text></dmn:outputEntry>
      </dmn:rule>
    </dmn:decisionTable>
  </dmn:decision>
"""
    )
    with pytest.raises(DeploymentException, match="aggregation 仅在"):
        parse_dmn_xml(xml)


def test_unknown_aggregation_rejected():
    xml = simple_table().replace(
        '<dmn:decisionTable hitPolicy="UNIQUE">',
        '<dmn:decisionTable hitPolicy="COLLECT" aggregation="MEDIAN">',
    )
    with pytest.raises(DeploymentException, match="未知 aggregation"):
        parse_dmn_xml(xml)


def test_entry_count_mismatch_rejected():
    xml = wrap(
        """
  <dmn:decision id="d1">
    <dmn:decisionTable hitPolicy="FIRST">
      <dmn:input id="i1"><dmn:inputExpression>a</dmn:inputExpression></dmn:input>
      <dmn:input id="i2"><dmn:inputExpression>b</dmn:inputExpression></dmn:input>
      <dmn:output id="o1" name="g"/>
      <dmn:rule id="r1">
        <dmn:inputEntry><dmn:text>1</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>1</dmn:text></dmn:outputEntry>
      </dmn:rule>
    </dmn:decisionTable>
  </dmn:decision>
"""
    )
    with pytest.raises(DeploymentException, match="inputEntry 数.*不一致"):
        parse_dmn_xml(xml)


def test_no_output_column_rejected():
    xml = wrap(
        """
  <dmn:decision id="d1">
    <dmn:decisionTable hitPolicy="FIRST">
      <dmn:input id="i1"><dmn:inputExpression>a</dmn:inputExpression></dmn:input>
      <dmn:rule id="r1">
        <dmn:inputEntry><dmn:text>1</dmn:text></dmn:inputEntry>
        <dmn:outputEntry><dmn:text>1</dmn:text></dmn:outputEntry>
      </dmn:rule>
    </dmn:decisionTable>
  </dmn:decision>
"""
    )
    with pytest.raises(DeploymentException, match="至少需要一个 output"):
        parse_dmn_xml(xml)


def test_dmn11_namespace_also_accepted():
    """文档化宽容：DMN 1.1 与 1.3 命名空间同构，均按 localName 解析。"""
    xml = wrap(
        """
  <decision xmlns="http://www.omg.org/spec/DMN/20151101/dmn.xsd" id="d1">
    <decisionTable hitPolicy="FIRST">
      <input id="i1"><inputExpression>a</inputExpression></input>
      <output id="o1" name="g"/>
      <rule id="r1">
        <inputEntry><text>1</text></inputEntry>
        <outputEntry><text>"x"</text></outputEntry>
      </rule>
    </decisionTable>
  </decision>
"""
    )
    assert parse_dmn_xml(xml).decision_keys() == ["d1"]
