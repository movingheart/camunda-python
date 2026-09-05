"""model 包：纯数据模型层（不依赖 DB / 不依赖解析器）。

对齐 Camunda bpmn-model：
- BpmnModel        := BpmnModelInstance 等价物，一个部署单元内可含多个 Process
- Process          := BPMN 流程定义（含 flow_nodes / sequence_flows）
- FlowNode         := 活动节点基类（事件/任务/网关）
- SequenceFlow     := 连线（可带 conditionExpression / default 标记）
"""

from camunda.model.bpmn import (
    BpmnModel,
    Process,
    FlowNode,
    SequenceFlow,
    TimerDefinition,
    StartEvent,
    EndEvent,
    UserTask,
    ServiceTask,
    ExclusiveGateway,
    ParallelGateway,
    IntermediateCatchEvent,
    BoundaryEvent,
)
from camunda.model.execution import Execution, ProcessInstance, ActivityInstance
from camunda.model.task import Task
from camunda.model.job import Job

__all__ = [
    "BpmnModel",
    "Process",
    "FlowNode",
    "SequenceFlow",
    "TimerDefinition",
    "StartEvent",
    "EndEvent",
    "UserTask",
    "ServiceTask",
    "ExclusiveGateway",
    "ParallelGateway",
    "IntermediateCatchEvent",
    "BoundaryEvent",
    "Execution",
    "ProcessInstance",
    "ActivityInstance",
    "Task",
    "Job",
]
