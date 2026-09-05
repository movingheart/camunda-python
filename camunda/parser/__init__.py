"""parser 包：BPMN 2.0 XML 解析（lxml 自研，对齐 Camunda bpmn-model 职责）。"""

from camunda.parser.bpmn_parser import parse_bpmn_xml, parse_bpmn_file

__all__ = ["parse_bpmn_xml", "parse_bpmn_file"]
