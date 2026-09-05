"""BPMN 数据模型（对齐 Camunda bpmn-model 职责，纯 dataclass，无解析/无 DB 依赖）。

设计要点：
- BpmnModel 是「部署单元」：一份 *.bpmn 文件可含多个 Process（BpmnModelInstance 等价物）
- FlowNode 有类型层级，但 M1 采用「注册表 + 运行时行为分派」，见 engine/behavior.py
- 所有元素的子元素/属性，只保留引擎流转所需字段，扩展属性走 extension 字典
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 定时器定义（M3）
# ---------------------------------------------------------------------------
@dataclass
class TimerDefinition:
    """BPMN timerEventDefinition 的语义承载。

    kind: "duration" | "date" | "cycle"（对应 timeDuration / timeDate / timeCycle 子元素）
    value: 原始文本（如 PT30S / 2026-09-03T09:00:00 / R3/PT10S 或 cron）
    delay_seconds: 仅 duration：value 解析后的秒数（解析期算好，运行时免重解析）
    """

    kind: str
    value: str
    delay_seconds: Optional[float] = None


# ---------------------------------------------------------------------------
# 多实例循环特征（M4-2c）
# ---------------------------------------------------------------------------
@dataclass
class MultiInstance:
    """BPMN multiInstanceLoopCharacteristics 的语义承载（挂活动节点）。

    实例集来源二选一（同时提供 collection 优先于 cardinality）：
    - collection_expr：camunda:collection 属性（形如 "${reviewers}"，运行时求值
      须得 list/tuple/set -> N 个实例；元素变量 element_variable 逐实例注入）
    - loop_cardinality_expr：bpmn:loopCardinality 子元素（形如 "3" 或 "${count}"，
      运行时求值须得 int -> N 个实例，无元素变量）
    都缺省 = 空集合（0 次）语义，运行时部署报错（Camunda 亦要求其一）。
    sequential: isSequential（true=顺序循环单线推进；false=并行 spawn N 实例）
    completion_condition_expr: completionCondition 子元素原文（形如
      "${nrOfCompletedInstances >= 2}"）。None = 全部实例完成后活动才结束。
    执行期内置计数器变量（引擎维护，与 Camunda 对齐）：
      nrOfInstances / nrOfActiveInstances / nrOfCompletedInstances / loopCounter。
    """

    sequential: bool = False
    collection_expr: Optional[str] = None
    loop_cardinality_expr: Optional[str] = None
    element_variable: Optional[str] = None
    completion_condition_expr: Optional[str] = None


# ---------------------------------------------------------------------------
# 连线
# ---------------------------------------------------------------------------
@dataclass
class SequenceFlow:
    """BPMN sequenceFlow：连接两个 FlowNode，可带条件（排他网关出边）。"""

    id: str
    source_ref: str
    target_ref: str
    name: Optional[str] = None
    # Camunda 语义：conditionExpression 文本（形如 "${amount > 1000}"），或 None
    condition_expression: Optional[str] = None


# ---------------------------------------------------------------------------
# 节点（FlowNode）
# ---------------------------------------------------------------------------
@dataclass
class FlowNode:
    """流程图中所有「节点」的基类：事件 / 任务 / 网关。"""

    id: str
    name: Optional[str] = None
    incoming: List[str] = field(default_factory=list)   # 入边 sequenceFlow id
    outgoing: List[str] = field(default_factory=list)   # 出边 sequenceFlow id
    # Camunda 排他网关默认边：default attribute（schema 层面叫 default）
    default_flow: Optional[str] = None
    # Camunda async continuation：asyncBefore 把「节点行为执行」拆成独立 job
    # （M3 实现 asyncBefore；asyncAfter 解析保留，行为 M4 支持，文档化差异）
    async_before: bool = False
    async_after: bool = False
    # 挂在本活动上的边界事件 id（boundaryEvent attachedToRef 归属，解析期回填）
    boundary_events: List[str] = field(default_factory=list)
    # 多实例循环特征（multiInstanceLoopCharacteristics，M4-2c；None = 普通活动）
    multi_instance: Optional[MultiInstance] = None
    # 附加属性（camunda:xxx 扩展 / 其它命名空间），引擎按需读取
    extension: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StartEvent(FlowNode):
    """流程起点 / 事件子流程触发器。一个 Process 有 0..N 个。

    事件槽互斥（恰好一个，解析校验）：
    - timer：定时触发（M3 流程级 timer start；M4-2b 事件子流程 timer start）
    - error_code：错误触发（仅事件子流程合法，M4-2b；规范强制 is_interrupting=True）
    - message_name：消息触发（事件子流程 message start，M4-2d；宿主 scope 激活期
      订阅，correlate_message 关联触发）
    - signal_name：信号触发（事件子流程 signal start，M4-2d；throw_signal 广播触发）
    流程级（非事件子流程）message/signal start 引擎不落地（M4-2d 文档化差异）。
    is_interrupting：事件子流程 start 的中断标志（true=取消宿主 scope 全部执行；
    false=spawn 并发执行，可多次触发）。普通流程 start 恒为 true（无宿主可中断）。
    """

    is_interrupting: bool = True
    # 事件槽（四选一，见类 docstring）
    timer: Optional[TimerDefinition] = None
    error_code: Optional[str] = None
    message_name: Optional[str] = None
    signal_name: Optional[str] = None


@dataclass
class IntermediateCatchEvent(FlowNode):
    """中间捕获事件（timer / message / signal 变体）。

    token 到达后停等（事件槽互斥）：
    - timer：注册 timer-catch Job（M3），到期触发继续；
    - message_name：注册消息订阅（M4-2d），correlate_message 关联触发；
    - signal_name：注册信号订阅（M4-2d），throw_signal / 实例内 signal throw 触发。
    """

    timer: Optional[TimerDefinition] = None
    message_name: Optional[str] = None
    signal_name: Optional[str] = None


@dataclass
class BoundaryEvent(FlowNode):
    """边界事件（timer / message / signal 变体，M4-1 起）。

    挂在宿主活动（attachedToRef）上，宿主等待期间事件触发即沿本事件的
    出边走绑定路径：
    - cancel_activity=True（默认）中断式：取消宿主活动（结算 actinst、删除
      宿主 task / 停等 job / 订阅），token 复用走边界出边；
    - cancel_activity=False 非中断式：宿主不取消，spawn 并发线从边界出边走
      （M4-2b4 timer；M4-2d message/signal 同规则——宿主仍在等待，订阅常驻
      可再次触发）。
    事件槽互斥（事件定义解析校验）：timer（M4-1）/ message、signal（M4-2d）。
    timer 仅支持 timeDuration / timeDate；timeCycle 拒绝（文档化差异）。
    """

    attached_to: Optional[str] = None  # 宿主活动节点 id（attachedToRef）
    cancel_activity: bool = True  # cancelActivity 属性（默认 true）
    timer: Optional[TimerDefinition] = None
    message_name: Optional[str] = None
    signal_name: Optional[str] = None


@dataclass
class SubProcess(FlowNode):
    """内嵌子流程（embedded subProcess，M4-2a）。

    SubProcess 是父容器（process 或上层 subProcess）里的一个活动节点，自身又是
    一个独立容器：process 字段持有内部 flow_nodes/sequence_flows/start_events
    （结构与 Process 相同）。执行语义：
    - token 到达 SubProcess -> 停驻为 SCOPE + spawn 内部 token 从内部 startEvent
      推进；内部全部走完（子 scope 收束）后父 token 复活沿 SubProcess 出边继续。
    - 边界事件可挂 SubProcess（attachedToRef），等待窗口 = 整段子流程执行期
      （M4-2a 中断式；cancelActivity=false 非中断式随事件子流程 M4-2b 落地）。
    - 变量作用域沿用实例级（M1 文档化差异，无子作用域遮蔽）。
    - triggered_by_event=true（事件子流程）解析保留、运行时明确报错（M4-2b）。
    容器间连线约束：sequenceFlow 只能引用同容器内的节点（跨容器 wire 部署即报错）。
    """

    # 内部容器（不参与 deploy 的独立 Process 实例；递归嵌套时其 flow_nodes 可再含 SubProcess）
    process: Optional["Process"] = None
    triggered_by_event: bool = False  # 事件子流程（Event SubProcess）标志


@dataclass
class EndEvent(FlowNode):
    """流程终点：token 到达即该 execution 结束（可带 throw 事件，互斥）。

    - error_code：错误结束（M4-2b）——token 结束同时抛 BPMN 错误，沿宿主 scope
      链冒泡找匹配的 error 事件子流程；无匹配等同 none end（对齐 Camunda）。
    - message_name：消息结束（M4-2d）——token 结束同时向本实例投递消息
      （1:1 就近关联，无匹配静默丢弃，对齐 Camunda 实例内 throw 语义）。
    - signal_name：信号结束（M4-2d）——token 结束同时在本实例内广播信号。
    """

    error_code: Optional[str] = None
    message_name: Optional[str] = None
    signal_name: Optional[str] = None


@dataclass
class IntermediateThrowEvent(FlowNode):
    """中间抛出事件（M4-2d：message / signal throw）。

    token 经过时不产生等待窗口：抛出的消息/信号在本实例内触发匹配订阅后，
    token 沿出边继续流转（无出边则收束）。消息 = 1:1 就近关联；信号 = 实例内
    广播（M4-2d 文档化差异：signal 跨实例广播由公共 API throw_signal 提供，
    throw 事件本身只广播到本实例）。
    事件槽互斥：message_name / signal_name 二选一；timer / error throw 不支持
    （解析期明确报错，文档化差异）。
    """

    message_name: Optional[str] = None
    signal_name: Optional[str] = None


@dataclass
class UserTask(FlowNode):
    """人工任务：引擎创建 Task 等待 complete，M1 不支持 assignee 表达式。"""

    assignee: Optional[str] = None
    candidate_users: List[str] = field(default_factory=list)
    candidate_groups: List[str] = field(default_factory=list)


@dataclass
class ServiceTask(FlowNode):
    """服务任务：实现 = delegate bean 名或 python 可调用注册名。

    解析规则（Camunda 兼容）：
    - camunda:delegateExpression="${myBean}" -> 注册名 myBean
    - camunda:class="com.foo.Bar"            -> 注册名取最后一个点号后段（对齐 Java 类短名）
    - 均未指定 -> extension["implementation_ref"] = None，行为默认 pass-through
    """

    implementation_ref: Optional[str] = None


@dataclass
class BusinessRuleTask(FlowNode):
    """业务规则任务（M5）：调用已部署的 DMN 决策并把结果写入实例变量。

    - decision_ref：camunda:decisionRef（部署即必填校验，缺失报错）
    - result_variable：camunda:resultVariable（默认 "result"），承接决策
      求值结果（标量/dict/列表，形态由 hitPolicy 决定，见 dmn/engine docstring）
    求值为同步无等待窗口（同 serviceTask）；DMN 部署不落库（对齐 delegate
    注册不落库先例，重启后须重新 deploy_dmn，文档化差异）。
    """

    decision_ref: Optional[str] = None
    result_variable: str = "result"


@dataclass
class ExclusiveGateway(FlowNode):
    """排他网关（XOR）：按条件取第一条满足的出边；无条件边兜底。"""


@dataclass
class ParallelGateway(FlowNode):
    """并行网关（AND）：fork 拆分多 token；join 汇聚（M1 单层 join 语义）。"""


# tag(localName) -> 节点类 的映射，供解析器分派
FLOW_NODE_TYPES: Dict[str, type] = {
    "startEvent": StartEvent,
    "endEvent": EndEvent,
    "userTask": UserTask,
    "serviceTask": ServiceTask,
    "businessRuleTask": BusinessRuleTask,
    "exclusiveGateway": ExclusiveGateway,
    "parallelGateway": ParallelGateway,
    "intermediateCatchEvent": IntermediateCatchEvent,
    "intermediateThrowEvent": IntermediateThrowEvent,
    "boundaryEvent": BoundaryEvent,
    "subProcess": SubProcess,
}


# ---------------------------------------------------------------------------
# 流程与模型
# ---------------------------------------------------------------------------
@dataclass
class Process:
    """BPMN process 定义（对齐 Camunda ProcessDefinition 的静态部分）。"""

    id: str                       # XML process id（Camunda 称之为 key）
    name: Optional[str] = None
    is_executable: bool = True
    # 元素索引
    flow_nodes: Dict[str, FlowNode] = field(default_factory=dict)      # id -> FlowNode
    sequence_flows: Dict[str, SequenceFlow] = field(default_factory=dict)
    # 便捷：按类型分组
    start_events: List[StartEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.flow_nodes:
            self.start_events = [
                n for n in self.flow_nodes.values() if isinstance(n, StartEvent)
            ]

    def get_flow_node(self, node_id: str) -> FlowNode:
        """按 id 取节点，缺失抛 KeyError（解析校验后正常流程不会发生）。"""
        return self.flow_nodes[node_id]

    def outgoing_of(self, node: FlowNode) -> List[SequenceFlow]:
        """某节点的全部出边（按 XML 出现顺序保持）。"""
        return [self.sequence_flows[fid] for fid in node.outgoing]

    def incoming_of(self, node: FlowNode) -> List[SequenceFlow]:
        return [self.sequence_flows[fid] for fid in node.incoming]


@dataclass
class BpmnModel:
    """一份 *.bpmn 部署单元（BpmnModelInstance 等价物）。"""

    processes: List[Process] = field(default_factory=list)
    # 源 XML 名（部署展示用，非必须）
    source_name: Optional[str] = None
    # 原始 XML 文本（M2 持久化时保存，恢复时可重新解析）
    source_xml: Optional[str] = None

    def get_process(self, process_key: str) -> Process:
        for p in self.processes:
            if p.id == process_key:
                return p
        raise KeyError(f"process not found in model: {process_key!r}")

    def process_keys(self) -> List[str]:
        return [p.id for p in self.processes]
