"""BPMN 2.0 XML 解析器（lxml）。

职责（对齐 Camunda bpmn-model 的解析部分）：
1. 解析 XML -> BpmnModel（含多个 Process）
2. 每个 Process 内：先收集 sequenceFlow，再按 tag 分派 flowNode 类型
3. 处理 camunda 扩展属性：delegateExpression / class（serviceTask 实现解析）
4. 校验：节点入边/出边引用存在、流程至少一个 startEvent

命名空间处理策略：只用 localName 分派（BPMN 语义元素），属性同时接受
camunda:xxx 与自带命名空间前缀。extensionElements 仅 serviceTask 需要，
取 camunda:class / camunda:delegateExpression。
"""

from __future__ import annotations

from typing import Dict, Optional

from lxml import etree

from camunda.common.exceptions import DeploymentException
from camunda.common.timers import parse_iso_duration
from camunda.model.bpmn import (
    BpmnModel,
    BoundaryEvent,
    BusinessRuleTask,
    EndEvent,
    MultiInstance,
    Process,
    SequenceFlow,
    FLOW_NODE_TYPES,
    FlowNode,
    IntermediateCatchEvent,
    IntermediateThrowEvent,
    ServiceTask,
    StartEvent,
    SubProcess,
    TimerDefinition,
    UserTask,
)

# BPMN 默认命名空间（用于无前缀 tag 匹配，localName 分派时其实不需要，保留常量以文档化）
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
CAMUNDA_NS = "http://camunda.org/schema/1.0/bpmn"

# timerEventDefinition 子元素名 -> kind
_TIMER_KIND = {
    "timeDuration": "duration",
    "timeDate": "date",
    "timeCycle": "cycle",
}


def _local(tag: str) -> str:
    """lxml tag 形如 {ns}localName -> localName。"""
    return tag.rsplit("}", 1)[-1]


def parse_bpmn_xml(xml_text: str, source_name: Optional[str] = None) -> BpmnModel:
    """解析 BPMN 2.0 XML 文本 -> BpmnModel。失败抛 DeploymentException。"""
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise DeploymentException(f"BPMN XML 语法错误: {e}") from e

    model = BpmnModel(source_name=source_name, source_xml=xml_text)

    # 顶层事件声明收集：<definitions> 下的 <error id errorCode> / <message id name> /
    # <signal id name>（errorEventDefinition errorRef / messageEventDefinition messageRef /
    #  signalEventDefinition signalRef 解析期回填，模型不保留声明表——事件槽内联
    #  code/name，运行时零查找）
    error_by_id: Dict[str, str] = {}
    message_by_id: Dict[str, str] = {}
    signal_by_id: Dict[str, str] = {}
    for el in root.iter():
        if not isinstance(el.tag, str):  # 跳过注释/PI 节点
            continue
        ln = _local(el.tag)
        if ln == "error":
            error_by_id[el.get("id", "")] = el.get("errorCode") or el.get("id") or ""
        elif ln == "message":
            message_by_id[el.get("id", "")] = el.get("name") or el.get("id") or ""
        elif ln == "signal":
            signal_by_id[el.get("id", "")] = el.get("name") or el.get("id") or ""

    # 顶层按出现顺序找 <process>（注意可能嵌套在 collaboration 外或内，直接遍历即可）
    for process_el in root.iter():
        if not isinstance(process_el.tag, str):  # 跳过注释/PI 节点
            continue
        if _local(process_el.tag) != "process":
            continue
        model.processes.append(
            _parse_process(process_el, error_by_id, message_by_id, signal_by_id)
        )

    if not model.processes:
        raise DeploymentException("BPMN 文件中未找到任何 <process> 元素")

    return model


def parse_bpmn_file(path: str) -> BpmnModel:
    with open(path, "r", encoding="utf-8") as f:
        return parse_bpmn_xml(f.read(), source_name=path)


# ---------------------------------------------------------------------------
# Process 解析
# ---------------------------------------------------------------------------
def _parse_process(
    process_el, error_by_id=None, message_by_id=None, signal_by_id=None
) -> Process:
    proc = Process(
        id=process_el.get("id") or process_el.get("name") or "process",
        name=process_el.get("name"),
        is_executable=(process_el.get("isExecutable", "true").lower() == "true"),
    )
    _fill_container(
        process_el,
        proc,
        error_by_id=error_by_id,
        message_by_id=message_by_id,
        signal_by_id=signal_by_id,
    )
    proc.__post_init__()  # 重算 start_events
    return proc


def _fill_container(
    el,
    proc: Process,
    require_start: bool = True,
    error_by_id: Optional[Dict[str, str]] = None,
    message_by_id: Optional[Dict[str, str]] = None,
    signal_by_id: Optional[Dict[str, str]] = None,
) -> None:
    """把一个 BPMN 容器元素（<process> 或 <subProcess>）的节点/连线解析进 Process。

    四遍结构（M4-2a 起容器递归）：
    1. sequenceFlow 先行（节点只存引用 id）
    2. flowNode / subProcess：subProcess 先登记为父容器节点，再递归解析其内部容器
    3. incoming/outgoing 连线挂接（同容器校验：跨容器引用自然报错）
    4. 边界事件按 attachedToRef 归属到宿主
    require_start=False 用于事件子流程（内部 start 是事件触发入口，独立校验）。
    结束调 __post_init__ 重算 start_events。
    """
    # 第一遍：sequenceFlow（连线必须先于节点存在，节点只存引用 id）
    for child in el:
        if not isinstance(child.tag, str):  # 跳过注释/PI 节点
            continue
        if _local(child.tag) == "sequenceFlow":
            flow = _parse_sequence_flow(child)
            proc.sequence_flows[flow.id] = flow

    # 第二遍：flowNode 家族（task/event/gateway/subProcess 等）
    for child in el:
        if not isinstance(child.tag, str):
            continue
        node_type = _local(child.tag)
        if node_type == "subProcess":
            sub = _parse_sub_process(child, error_by_id, message_by_id, signal_by_id)
            proc.flow_nodes[sub.id] = sub
        elif node_type in FLOW_NODE_TYPES:
            node = _parse_flow_node(child, node_type, error_by_id, message_by_id, signal_by_id)
            proc.flow_nodes[node.id] = node

    # 第三遍：把连线引用挂到节点上（incoming/outgoing 子元素按 XML 顺序）
    _wire_flows(proc)
    # 第四遍：边界事件按 attachedToRef 归属到宿主活动
    _attach_boundaries(proc)

    # 校验 + 重算 start_events
    _validate_process(proc, require_start=require_start)
    proc.__post_init__()


def _parse_sub_process(
    el, error_by_id=None, message_by_id=None, signal_by_id=None
) -> SubProcess:
    """解析 <subProcess>：父容器登记节点 + 递归解析内部容器。

    通用属性（id/name/camunda:asyncBefore 等）走 _parse_flow_node；内部子元素
    （内部 sequenceFlow / flowNode / 嵌套 subProcess / boundary 归属）递归进
    _fill_container。事件子流程（triggeredByEvent=true）：内部 start 事件驱动
    （无普通 startEvent），解析后做事件子流程专项校验（_validate_event_subprocess）。
    """
    node = _parse_flow_node(el, "subProcess", error_by_id, message_by_id, signal_by_id)
    assert isinstance(node, SubProcess)
    node.triggered_by_event = el.get("triggeredByEvent", "false").lower() == "true"
    inner = Process(id=f"{node.id}::inner", name=node.name, is_executable=True)
    _fill_container(
        el,
        inner,
        require_start=not node.triggered_by_event,
        error_by_id=error_by_id,
        message_by_id=message_by_id,
        signal_by_id=signal_by_id,
    )
    node.process = inner
    if node.triggered_by_event:
        _validate_event_subprocess(node, inner)
    return node


def _validate_event_subprocess(sub: SubProcess, inner: Process) -> None:
    """事件子流程容器规范校验（triggeredByEvent=true，M4-2b）。

    事件子流程不参与 sequenceFlow 流转（无 incoming/outgoing），内部 startEvent
    是唯一触发入口，因此：
    - 至少一个 startEvent；
    - 每个 startEvent 必须恰好带一个事件定义（timer / error / message / signal）——
      none start 无法触发事件子流程；
    - error start 强制 isInterrupting=true（BPMN 规范：错误事件只能中断式）；
    - startEvent 不得有入边（事件子流程不与 sequenceFlow 相连）。
    message/signal start 解析与运行时订阅 M4-2d 落地（correlate_message /
    throw_signal 触发，中断/非中断随 isInterrupting）。
    """
    starts = [n for n in inner.flow_nodes.values() if isinstance(n, StartEvent)]
    if not starts:
        raise DeploymentException(
            f"事件子流程 {sub.id!r} 缺少 startEvent（事件触发入口）"
        )
    for st in starts:
        has_event = (
            st.timer is not None
            or st.error_code is not None
            or st.message_name is not None
            or st.signal_name is not None
        )
        if not has_event:
            raise DeploymentException(
                f"事件子流程 {sub.id!r} 的 startEvent {st.id!r} 缺少事件定义"
                "（none start 不能触发事件子流程）"
            )
        if st.error_code is not None and not st.is_interrupting:
            raise DeploymentException(
                f"事件子流程 {sub.id!r} 的 error start {st.id!r} 声明 "
                "isInterrupting=false：BPMN 规范错误事件只支持中断式"
            )
        if st.incoming:
            raise DeploymentException(
                f"事件子流程 {sub.id!r} 的 startEvent {st.id!r} 有入边："
                "事件子流程不参与 sequenceFlow 流转"
            )


def _parse_sequence_flow(el) -> SequenceFlow:
    flow = SequenceFlow(
        id=el.get("id", ""),
        source_ref=el.get("sourceRef", ""),
        target_ref=el.get("targetRef", ""),
        name=el.get("name"),
    )
    # conditionExpression 是 sequenceFlow 的子元素：<conditionExpression xsi:type="tFormalExpression">${...}</...>
    for child in el:
        if not isinstance(child.tag, str):
            continue
        if _local(child.tag) == "conditionExpression":
            text = (child.text or "").strip()
            if text:
                flow.condition_expression = text
    return flow


def _parse_flow_node(
    el, node_type: str, error_by_id=None, message_by_id=None, signal_by_id=None
) -> FlowNode:
    cls = FLOW_NODE_TYPES[node_type]
    node_id = el.get("id", "")
    node = cls(
        id=node_id,
        name=el.get("name"),
        default_flow=el.get("default"),
    )
    # 收集 camunda 命名空间直接属性（如 camunda:class / camunda:delegateExpression
    # 常直接写在元素属性上，也可能藏在 extensionElements 子元素里——两者都收）
    camunda_attrs: Dict[str, Optional[str]] = {}
    for attr_name, attr_val in el.attrib.items():
        if "}" in attr_name and attr_name.rsplit("}", 1)[0].strip("{}") == CAMUNDA_NS:
            camunda_attrs[attr_name.rsplit("}", 1)[-1]] = attr_val.strip()

    # async continuation 标志（camunda:asyncBefore / asyncAfter="true"）
    if "asyncBefore" in camunda_attrs:
        node.async_before = camunda_attrs["asyncBefore"].lower() == "true"
    if "asyncAfter" in camunda_attrs:
        node.async_after = camunda_attrs["asyncAfter"].lower() == "true"

    # 解析子元素：incoming/outgoing 由 _wire_flows 回填，此处仅处理 extensionElements
    _parse_node_children(el, node)

    # boundaryEvent: attachedToRef / cancelActivity（事件定义解析走下面事件共通路径）
    if isinstance(node, BoundaryEvent):
        node.attached_to = el.get("attachedToRef")
        node.cancel_activity = el.get("cancelActivity", "true").lower() != "false"

    # startEvent: isInterrupting（事件子流程中断/非中断标志；普通流程恒 true）
    if isinstance(node, StartEvent):
        node.is_interrupting = el.get("isInterrupting", "true").lower() != "false"

    # 事件类节点：事件定义统一解析（timer M3 / error、message M4-2b）
    if isinstance(
        node,
        (StartEvent, IntermediateCatchEvent, BoundaryEvent, EndEvent, IntermediateThrowEvent),
    ):
        timer, error_code, message_name, signal_name = _parse_event_definitions(
            el, error_by_id, message_by_id, signal_by_id
        )
        defined = [x is not None for x in (timer, error_code, message_name, signal_name)]
        if sum(defined) > 1:
            raise DeploymentException(
                f"节点 {node.id!r} 携带多个事件定义（timer/error/message/signal 互斥）"
            )
        if isinstance(node, (StartEvent, IntermediateCatchEvent, BoundaryEvent)):
            node.timer = timer
            node.error_code = error_code
            node.message_name = message_name
            node.signal_name = signal_name
        elif isinstance(node, (EndEvent, IntermediateThrowEvent)):
            # throw 类事件（M4-2d）：end/中间抛出支持 error/message/signal；
            # timer throw 无意义（拒绝）
            node.error_code = error_code
            node.message_name = message_name
            node.signal_name = signal_name
            if timer is not None:
                raise DeploymentException(
                    f"{type(node).__name__} {node.id!r} 不支持 timer throw 事件"
                    "（文档化差异）"
                )
        else:  # IntermediateCatchEvent / BoundaryEvent 之外的类型不会进入此分支
            raise DeploymentException(
                f"节点 {node.id!r} 类型 {type(node).__name__} 不支持事件定义"
            )

    # serviceTask: 解析实现引用（属性优先级高于扩展子元素）
    if isinstance(node, ServiceTask):
        ref = (
            camunda_attrs.get("delegateExpression")
            or camunda_attrs.get("class")
            or node.extension.get("delegateExpression")
            or node.extension.get("class")
        )
        node.implementation_ref = _resolve_impl_ref(ref)

    # businessRuleTask（M5）：DMN 决策引用必填；结果变量缺省 "result"
    if isinstance(node, BusinessRuleTask):
        ref = camunda_attrs.get("decisionRef") or node.extension.get("decisionRef")
        if not ref:
            raise DeploymentException(
                f"businessRuleTask {node.id!r} 缺少 camunda:decisionRef"
            )
        node.decision_ref = ref
        node.result_variable = (
            camunda_attrs.get("resultVariable")
            or node.extension.get("resultVariable")
            or "result"
        )

    # 其余 camunda 属性并入 extension（供未来里程碑使用）
    if camunda_attrs:
        node.extension.update(camunda_attrs)
    return node


def _parse_node_children(el, node: FlowNode) -> None:
    """解析节点子元素：incoming/outgoing 引用在 _wire_flows 处理；
    这里处理 extensionElements 里的简单 camunda:xxx 扩展与 multiInstanceLoopCharacteristics。"""
    camunda_attrs: Dict[str, Optional[str]] = {}

    # 遍历子元素（XML 顺序）
    for child in el:
        if not isinstance(child.tag, str):
            continue
        lname = _local(child.tag)
        # 注意：incoming/outgoing 引用由 _wire_flows 统一从 sequenceFlow 回填，
        # 这里不收集，避免与连线挂接逻辑重复。
        if lname in ("incoming", "outgoing"):
            continue
        if lname == "conditionExpression":
            # 条件表达式挂在 sequenceFlow 上，此处不会出现；防御性忽略
            pass
        elif lname == "multiInstanceLoopCharacteristics":
            # 多实例循环特征（M4-2c）：宿主白名单 userTask / serviceTask /
            # subProcess；其余类型（事件/网关等）部署即报错（文档化差异）
            if not isinstance(node, (UserTask, ServiceTask, SubProcess)):
                raise DeploymentException(
                    f"节点 {node.id!r} 声明 multiInstanceLoopCharacteristics：M4-2c "
                    f"仅支持 userTask / serviceTask / subProcess 宿主，"
                    f"{type(node).__name__} 不支持（文档化差异）"
                )
            node.multi_instance = _parse_multi_instance(child)
        elif lname == "extensionElements":
            _collect_camunda_extension(child, camunda_attrs)

    if camunda_attrs:
        node.extension.update(camunda_attrs)


def _parse_multi_instance(el) -> MultiInstance:
    """解析 bpmn:multiInstanceLoopCharacteristics 子元素。

    XML 结构：
        <bpmn:multiInstanceLoopCharacteristics isSequential="false"
            camunda:collection="${reviewers}" camunda:elementVariable="reviewer">
          <bpmn:loopCardinality xsi:type="bpmn:tFormalExpression">3</...>
          <bpmn:completionCondition xsi:type="bpmn:tFormalExpression">
            ${nrOfCompletedInstances >= 2}</...>
        </bpmn:multiInstanceLoopCharacteristics>

    表达式一律保留原文（含 ${}），求值由引擎在运行时做。collection 与
    loopCardinality 至少其一（同时提供 collection 优先），否则部署报错。
    """
    camunda_attrs: Dict[str, Optional[str]] = {}
    for attr_name, attr_val in el.attrib.items():
        if "}" in attr_name and attr_name.rsplit("}", 1)[0].strip("{}") == CAMUNDA_NS:
            camunda_attrs[attr_name.rsplit("}", 1)[-1]] = attr_val.strip()

    sequential = el.get("isSequential", "false").strip().lower() == "true"
    collection = camunda_attrs.get("collection")
    element_variable = camunda_attrs.get("elementVariable")
    cardinality: Optional[str] = None
    completion: Optional[str] = None
    for child in el:
        if not isinstance(child.tag, str):
            continue
        lname = _local(child.tag)
        if lname == "loopCardinality":
            cardinality = (child.text or "").strip()
        elif lname == "completionCondition":
            completion = (child.text or "").strip()
    if collection is None and cardinality is None:
        raise DeploymentException(
            "multiInstanceLoopCharacteristics 必须提供 camunda:collection 或 "
            "loopCardinality（M4-2c），当前两者皆缺"
        )
    if element_variable is not None and collection is None:
        raise DeploymentException(
            "camunda:elementVariable 仅与 camunda:collection 配合使用"
        )
    return MultiInstance(
        sequential=sequential,
        collection_expr=collection,
        loop_cardinality_expr=cardinality,
        element_variable=element_variable,
        completion_condition_expr=completion,
    )


def _collect_camunda_extension(extension_el, out: Dict[str, Optional[str]]) -> None:
    """从 extensionElements 里收集 camunda:* 子元素的文本值。

    camunda:properties / camunda:inputOutput 等复杂结构 M1 不解析，只收集简单标量。
    """
    for child in extension_el:
        lname = _local(child.tag)
        # 跳过复杂容器（properties/inputOutput/connector/field/script...），M4 再支持
        if lname in ("properties", "inputOutput", "connector", "field", "script"):
            continue
        text = (child.text or "").strip()
        # 只保留有文本的简单扩展，如 <camunda:failedJobRetryTimeCycle>PT5M</...>
        if text:
            out[lname] = text


def _parse_timer_definition(el) -> Optional[TimerDefinition]:
    """读事件元素里的 timerEventDefinition。

    XML 结构：
        <bpmn:intermediateCatchEvent id="wait">
          <bpmn:timerEventDefinition>
            <bpmn:timeDuration xsi:type="bpmn:tFormalExpression">PT30S</...>
    duration 在解析期算好 delay_seconds（运行时免重解析），非法时长部署期即报错。
    """
    for child in el:
        if not isinstance(child.tag, str):
            continue
        if _local(child.tag) != "timerEventDefinition":
            continue
        for sub in child:
            if not isinstance(sub.tag, str):
                continue
            kind = _TIMER_KIND.get(_local(sub.tag))
            if kind is None:
                continue
            text = (sub.text or "").strip()
            if not text:
                continue
            if kind == "duration":
                try:
                    delay = parse_iso_duration(text)
                except ValueError as e:
                    raise DeploymentException(
                        f"timer timeDuration 非法 {text!r}: {e}"
                    ) from e
                return TimerDefinition(kind="duration", value=text, delay_seconds=delay)
            return TimerDefinition(kind=kind, value=text)
    return None


def _parse_event_definitions(el, error_by_id=None, message_by_id=None, signal_by_id=None):
    """解析事件元素的全部事件定义 -> (timer, error_code, message_name, signal_name)。

    - timerEventDefinition    -> _parse_timer_definition（M3）
    - errorEventDefinition    -> errorRef 关联顶层 <error errorCode>，解析期回填 code
      （兼容直接在定义上写 errorCode 的建模工具；无 code 部署即报错）
    - messageEventDefinition  -> messageRef 关联顶层 <message name>，回填 name
      （兼容 messageEventDefinition 直接写 name）
    - signalEventDefinition   -> signalRef 关联顶层 <signal name>，回填 name
      （兼容 signalEventDefinition 直接写 name）（M4-2d）
    事件元素至多一个事件定义（互斥校验在调用方）。
    """
    timer = _parse_timer_definition(el)
    error_code = None
    message_name = None
    signal_name = None
    for child in el:
        if not isinstance(child.tag, str):
            continue
        ln = _local(child.tag)
        if ln == "errorEventDefinition":
            ref = child.get("errorRef")
            code = None
            if ref:
                code = (error_by_id or {}).get(ref)
                if code is None:
                    raise DeploymentException(
                        f"errorEventDefinition 引用未知 error 声明: {ref!r}"
                    )
            code = code or child.get("errorCode")
            if not code:
                raise DeploymentException(
                    "errorEventDefinition 缺少可解析的 errorCode（无 errorRef 指向 "
                    "的顶层声明，也未直接声明 errorCode）"
                )
            error_code = code
        elif ln == "messageEventDefinition":
            ref = child.get("messageRef")
            name = None
            if ref:
                name = (message_by_id or {}).get(ref)
                if name is None:
                    raise DeploymentException(
                        f"messageEventDefinition 引用未知 message 声明: {ref!r}"
                    )
            name = name or child.get("name")
            if not name:
                raise DeploymentException(
                    "messageEventDefinition 缺少可解析的 message name（无 messageRef "
                    "指向的顶层声明，也未直接声明 name）"
                )
            message_name = name
        elif ln == "signalEventDefinition":
            ref = child.get("signalRef")
            name = None
            if ref:
                name = (signal_by_id or {}).get(ref)
                if name is None:
                    raise DeploymentException(
                        f"signalEventDefinition 引用未知 signal 声明: {ref!r}"
                    )
            name = name or child.get("name")
            if not name:
                raise DeploymentException(
                    "signalEventDefinition 缺少可解析的 signal name（无 signalRef "
                    "指向的顶层声明，也未直接声明 name）"
                )
            signal_name = name
    return timer, error_code, message_name, signal_name


def _resolve_impl_ref(ref: Optional[str]) -> Optional[str]:
    """把 camunda 实现引用转成注册名。

    - delegateExpression="${myBean}"   -> "myBean"
    - class="com.example.MyDelegate"    -> "MyDelegate"（短名；M1 用短名注册 Python 可调用）
    """
    if not ref:
        return None
    ref = ref.strip()
    if ref.startswith("${") and ref.endswith("}"):
        return ref[2:-1].strip()
    if "." in ref:
        return ref.rsplit(".", 1)[-1]
    return ref


# ---------------------------------------------------------------------------
# 连线挂接与校验
# ---------------------------------------------------------------------------
def _wire_flows(proc: Process) -> None:
    """校验所有 sequenceFlow 的 source/target 存在于 flow_nodes，并回填节点出/入边。"""
    for flow in proc.sequence_flows.values():
        src = proc.flow_nodes.get(flow.source_ref)
        tgt = proc.flow_nodes.get(flow.target_ref)
        if src is None:
            raise DeploymentException(
                f"sequenceFlow {flow.id!r} 的 sourceRef {flow.source_ref!r} 不存在于 process {proc.id!r}"
            )
        if tgt is None:
            raise DeploymentException(
                f"sequenceFlow {flow.id!r} 的 targetRef {flow.target_ref!r} 不存在于 process {proc.id!r}"
            )
        src.outgoing.append(flow.id)
        tgt.incoming.append(flow.id)


def _attach_boundaries(proc: Process) -> None:
    """把 boundaryEvent 挂到宿主活动（attachedToRef 回填 flow_nodes[host].boundary_events）。

    边界事件不入主流转（无 incoming），仅出边参与流转；触发由引擎在宿主
    等待期间调度（见 engine timer-boundary job）。BPMN 约束：宿主必须是活动
    或事件（不能是 start/end/boundary/网关），M4-1 引擎进一步限制为有等待点
    的活动（userTask / asyncBefore 节点），运行时明确报错。
    """
    for node in proc.flow_nodes.values():
        if not isinstance(node, BoundaryEvent):
            continue
        if not node.attached_to:
            raise DeploymentException(
                f"boundaryEvent {node.id!r} 缺少 attachedToRef（未指定宿主活动）"
            )
        host = proc.flow_nodes.get(node.attached_to)
        if host is None:
            raise DeploymentException(
                f"boundaryEvent {node.id!r} 的 attachedToRef {node.attached_to!r} 不存在于 process {proc.id!r}"
            )
        if isinstance(host, (StartEvent, EndEvent, BoundaryEvent)):
            raise DeploymentException(
                f"boundaryEvent {node.id!r} 不能挂在 {type(host).__name__} {host.id!r} 上"
            )
        host.boundary_events.append(node.id)


def _validate_process(proc: Process, require_start: bool = True) -> None:
    """容器结构校验。

    require_start=False 用于事件子流程等「内部没有普通 startEvent」的容器
    （M4-2b 前宽容；运行时语义由引擎明确报错）。
    """
    if not proc.flow_nodes:
        raise DeploymentException(f"process {proc.id!r} 没有任何 flowNode")

    if require_start and not any(
        isinstance(n, StartEvent) for n in proc.flow_nodes.values()
    ):
        raise DeploymentException(f"process {proc.id!r} 缺少 startEvent")

    # 排他网关出边必须有 default 或条件（宽松校验：不强制，运行时无条件时随机走第一条 -> 引擎内决定）
