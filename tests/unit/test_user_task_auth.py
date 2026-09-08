"""userTask 任务归属求值：assignee / candidateUsers / candidateGroups。

M4-2 之前 userTask 上的 camunda:assignee / candidateUsers / candidateGroups
属性被静默忽略；M4-2 起 parser 抽取 + 引擎 _create_task 阶段对每项调用
evaluate_expression 求值，结果写入运行时 Task 字段。
"""

from __future__ import annotations

import pytest

from camunda.engine import ProcessEngine
from camunda.parser import parse_bpmn_xml

XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  targetNamespace="http://example">
  <bpmn:process id="auth" isExecutable="true">
    <bpmn:startEvent id="s"/>
    <bpmn:userTask id="submit"
        camunda:assignee="${startBy}"/>
    <bpmn:userTask id="approve"
        camunda:candidateUsers="${manager_users}"
        camunda:candidateGroups="finance,u005"/>
    <bpmn:endEvent id="e"/>
    <bpmn:sequenceFlow id="f1" sourceRef="s" targetRef="submit"/>
    <bpmn:sequenceFlow id="f2" sourceRef="submit" targetRef="approve"/>
    <bpmn:sequenceFlow id="f3" sourceRef="approve" targetRef="e"/>
  </bpmn:process>
</bpmn:definitions>
"""


def _engine_with_auth_deployed():
    model = parse_bpmn_xml(XML)
    engine = ProcessEngine()
    engine.deploy(model)
    return engine


def test_assignee_resolved_from_start_by_variable():
    """${startBy} 应在 userTask 创建时求值写回 Task.assignee。"""
    engine = _engine_with_auth_deployed()
    pi = engine.start_process_instance_by_key("auth", {"startBy": "u001"})
    tasks = engine.create_task_query(process_instance_id=pi.id)
    assert len(tasks) == 1 and tasks[0].task_definition_key == "submit"
    assert tasks[0].assignee == "u001"
    assert tasks[0].candidate_users == []
    assert tasks[0].candidate_groups == []


def test_candidate_users_and_groups_resolved_from_variables_and_literals():
    """candidateUsers 接收 ${var}（值为列表），candidateGroups 接收混合列表。"""
    engine = _engine_with_auth_deployed()
    pi = engine.start_process_instance_by_key(
        "auth",
        {
            "startBy": "u001",
            "manager_users": ["u002", "u003"],
        },
    )
    # 流程先停在 submit，complete 后进入 approve
    submit = engine.create_task_query(process_instance_id=pi.id)[0]
    engine.complete_task(submit.id, {"approved": True})
    tasks = engine.create_task_query(process_instance_id=pi.id)
    assert len(tasks) == 1 and tasks[0].task_definition_key == "approve"
    # ${manager_users} 求值后展平为 ["u002", "u003"]，与 BPMN 表达式一致
    assert tasks[0].candidate_users == ["u002", "u003"]
    # candidateGroups "finance,u005" 全是字面量，按字面保留
    assert tasks[0].candidate_groups == ["finance", "u005"]
    assert tasks[0].assignee is None  # 无 assignee 配置


def test_undefined_variable_leaves_field_empty():
    """引用未定义变量时降级为空（不抛异常、不阻塞 userTask 进入）。"""
    engine = _engine_with_auth_deployed()
    # startBy 缺失 -> assignee 留空
    pi = engine.start_process_instance_by_key("auth", {})
    submit = engine.create_task_query(process_instance_id=pi.id)[0]
    assert submit.assignee is None
    engine.complete_task(submit.id, {"approved": True})
    # manager_users 缺失 -> candidate_users 留空
    approve = engine.create_task_query(process_instance_id=pi.id)[0]
    assert approve.candidate_users == []
    # candidate_groups 是字面量，与变量状态无关
    assert approve.candidate_groups == ["finance", "u005"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
