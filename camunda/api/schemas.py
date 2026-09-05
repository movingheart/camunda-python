"""REST 层 DTO 与变量序列化（M6-1）。

变量形态（M6 关键兼容点，文档化差异见 docs/ARCHITECTURE.md）：
- Camunda 7 REST 用「包装形态」：{"amount": {"value": 20000, "type": "Long"}}
- 本实现**入参两种都收**：包装形态与裸值 {"amount": 20000} 均可（裸值按 Python 类型推断 type）
- 出参默认走包装形态（对齐 Camunda），带 `?bare=true` 时退化成裸值 map（便于脚本直用）

类型推断：Python 类型 -> Camunda 类型名（String/Boolean/Integer/Long/Double/Null/Object）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from camunda.model.execution import ProcessInstance
from camunda.model.task import Task

# ---------------------------------------------------------------------------
# 变量序列化
# ---------------------------------------------------------------------------
def _type_of(value: Any) -> str:
    """Python 值 -> Camunda 变量类型名。"""
    if value is None:
        return "Null"
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        return "Long"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, str):
        return "String"
    return "Object"  # dict / list / 其余：JSON 序列化对象


def to_variable_dto(value: Any) -> Dict[str, Any]:
    """单变量 -> Camunda VariableValueDto。"""
    dto: Dict[str, Any] = {"value": value, "type": _type_of(value)}
    if dto["type"] == "Object":
        dto["valueInfo"] = {"serializationDataFormat": "application/json"}
    return dto


def to_variable_map(variables: Optional[Dict[str, Any]], bare: bool = False) -> Dict[str, Any]:
    """变量 dict -> 响应形态（bare=True 时退化为裸值 map）。"""
    variables = variables or {}
    if bare:
        return dict(variables)
    return {k: to_variable_dto(v) for k, v in variables.items()}


def from_variable_map(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """请求变量 dict -> 引擎变量 dict（兼容包装形态与裸值）。

    包装形态：{"amount": {"value": 20000, "type": "Long"}} -> {"amount": 20000}
    裸值形态：{"amount": 20000}                            -> {"amount": 20000}
    """
    if not payload:
        return {}
    out: Dict[str, Any] = {}
    for name, spec in payload.items():
        if isinstance(spec, dict) and "value" in spec:
            out[name] = spec["value"]  # type 仅文档化，引擎按 Python 原生类型处理
        else:
            out[name] = spec
    return out


# ---------------------------------------------------------------------------
# 请求 DTO
# ---------------------------------------------------------------------------
class VariableValueDto(BaseModel):
    """Camunda VariableValueDto（入参；type/valueInfo 仅文档化，引擎按原生类型处理）。"""

    value: Any = None
    type: Optional[str] = None
    valueInfo: Optional[Dict[str, Any]] = None


class StartProcessInstanceDto(BaseModel):
    """POST /process-instance 请求体。variables 支持包装与裸值两种形态。

    definitionKey 必填（Camunda 还支持 definitionId / message 启动，M6 不支持——
    见 docs/ARCHITECTURE.md 的 M6 文档化差异）。
    """

    definitionKey: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None
    businessKey: Optional[str] = None
    withVariablesInReturn: Optional[bool] = False


class CompleteTaskDto(BaseModel):
    """POST /task/{id}/complete 请求体。"""

    variables: Optional[Dict[str, Any]] = None


class UserIdDto(BaseModel):
    """POST /task/{id}/claim|unclaim|assignee 请求体。"""

    userId: Optional[str] = None


class DeploymentDto(BaseModel):
    """POST /deployment/create（JSON 便捷通道；multipart 见路由说明）。"""

    xml: str = Field(..., description="BPMN 2.0 XML 文本")
    name: Optional[str] = None


class EvaluateDecisionDto(BaseModel):
    """POST /decision-definition/key/{key}/evaluate 请求体。"""

    variables: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# 响应 DTO（直接构造 dict，由 FastAPI 序列化）
# ---------------------------------------------------------------------------
def process_instance_dto(pi: ProcessInstance, bare: bool = False) -> Dict[str, Any]:
    """ProcessInstance -> Camunda ProcessInstanceDto。"""
    return {
        "id": pi.id,
        "definitionId": pi.process_definition_key,
        "businessKey": pi.business_key,
        "ended": pi.is_completed,
        "suspended": False,
        "variables": to_variable_map(pi.variables, bare=bare),
    }


def task_dto(t: Task) -> Dict[str, Any]:
    """Task -> Camunda TaskDto。"""
    return {
        "id": t.id,
        "name": t.name,
        "assignee": t.assignee,
        "created": t.create_time,
        "processInstanceId": t.process_instance_id,
        "executionId": t.execution_id,
        "taskDefinitionKey": t.task_definition_key,
        "candidateUsers": list(t.candidate_users),
        "candidateGroups": list(t.candidate_groups),
    }


def process_definition_dto(
    key: str, name: Optional[str], version: int
) -> Dict[str, Any]:
    """流程定义 -> Camunda ProcessDefinitionDto。"""
    return {
        "id": f"{key}:{version}",
        "key": key,
        "name": name,
        "version": version,
    }


def activity_instance_dto(ai: Any) -> Dict[str, Any]:
    """ActivityInstance -> Camunda HistoricActivityInstanceDto。"""
    return {
        "id": ai.id,
        "activityId": ai.activity_id,
        "activityName": ai.activity_name,
        "activityType": None,
        "processInstanceId": ai.process_instance_id,
        "executionId": ai.execution_id,
        "startTime": ai.start_time,
        "endTime": ai.end_time,
        "durationInMillis": None,
    }


def historic_task_dto(t: Task) -> Dict[str, Any]:
    """已归档 Task -> Camunda HistoricTaskInstanceDto。"""
    return {
        **task_dto(t),
        "endTime": t.end_time,
        "deleteReason": None,
    }


def historic_process_instance_dto(pi: ProcessInstance) -> Dict[str, Any]:
    """ProcessInstance -> Camunda HistoricProcessInstanceDto。"""
    return {
        "id": pi.id,
        "processDefinitionKey": pi.process_definition_key,
        "businessKey": pi.business_key,
        "startTime": pi.start_time,
        "endTime": pi.end_time,
        "state": pi.state.value if hasattr(pi.state, "value") else str(pi.state),
    }


def decision_definition_dto(key: str, version: int, name: Optional[str]) -> Dict[str, Any]:
    """决策定义 -> Camunda DecisionDefinitionDto。"""
    return {"id": f"{key}:{version}", "key": key, "name": name, "version": version}
