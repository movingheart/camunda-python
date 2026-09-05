"""ProcessEngine 门面：流程引擎（M1 内存 / M2 可选持久化 / M3 作业）。

架构对齐（见 docs/ARCHITECTURE.md）：
- RepositoryService 语义 -> deploy() / get_process_definition()
- RuntimeService 语义   -> start_process_instance_by_key() / get_process_instance()
- TaskService 语义      -> complete_task() / create_task_query()

执行模型（语义对齐 Camunda PVM 的核心）：
- **Execution 树**：实例根 Execution 下按需挂子 Execution。并行网关 fork 时
  父 execution 转 SCOPE 停驻，每条出边 spawn 一个子 TOKEN execution。
- **队列推进**：token 到达节点产生新到达事件，入队循环 pump，天然支撑并发。
- **并行网关 join**：实例级 join_arrivals[gw_id] 登记到达 token 数；到达数 ==
  网关入边数 时汇聚：清停等 token、SCOPE 恢复沿网关出边继续。
  （M1 约束：无循环回连并行网关、并行分支内不嵌套并行，M4 强化。）
- **变量作用域**：全放 ProcessInstance.variables（实例级）。
- **活动历史**：ActivityInstance 进/出痕迹（内存 + M2 落 ACT_HI_ACTINST）。

M2 持久化（事务边界同步）：
- deploy / start / complete 三个命令结束后把实例状态全量同步到 ACT 表；
  进程崩溃 => 未完成命令整体丢失（等价 Camunda 单命令事务语义）。
- from_database() 从 ACT_RU_* 恢复活跃实例（execution 树/task/变量/join 等待）。

M3 作业（Job / Timer / async continuation）：
- token 到达 timer 中间捕获事件 -> 停等 + 注册 timer-catch Job（duedate）-> 到期
  execute_job 让 token 继续；Timer Start 为定义级 timer-start Job，触发即启动实例。
- camunda:asyncBefore 把节点行为执行拆成 async-continuation Job（Camunda async
  continuation 语义）；execute_job 失败 -> retries-1 -> 到期重试；retries 耗尽 =
  死信（不再 acquire，实例级失败时若启用 store 自动回滚内存到上次同步点）。

M4-1 扩展：
- timer 边界事件（timer-boundary Job）：宿主 = 有等待点的活动
  （userTask / asyncBefore 节点）。中断式（cancelActivity=true）等待期内到点
  触发即取消宿主、token 改走边界事件出边；非中断式（cancelActivity=false，
  M4-2b4 落地）到点触发不取消宿主，spawn 并发线从边界事件出边走（单发，宿主
  继续等待，可多次触发不同边界/同一边界不同窗口——timeCycle 不支持，文档化
  差异）。宿主正常离开撤销边界 Job。
- camunda:asyncAfter 把「离开推进」拆成 async-after Job（支持 serviceTask /
  exclusiveGateway；XOR 离开时重新求值出边条件；其余类型明确报错——文档化差异）。
  可与 asyncBefore 链式。
- camunda:asyncAfter 把「离开推进」拆成 async-after Job（支持 serviceTask /
  exclusiveGateway；XOR 离开时重新求值出边条件；其余类型明确报错——文档化差异）。
  可与 asyncBefore 链式。

M4-2a 扩展（embedded SubProcess 容器语义）：
- 容器感知流转：token 可能在内嵌子流程内部推进，节点/连线/边界作业归属一律按
  token 所在容器（_container_of：沿父链找最近停驻在 SubProcess 的 SCOPE 祖先，
  取其 inner Process；否则根 Process）解析，跨容器节点 id 不串扰。
- 进入 subProcess：token 转 SCOPE 停驻（activity=subProcess id，actinst 跨整段
  内部执行期 open），spawn 内部子 token 从内部 startEvent 推进。
- 收束复活：内部全部走完（含并行分支直通 end 的逐层 SCOPE 收束）后，subProcess
  SCOPE 无活跃子 -> 结算 actinst、恢复 TOKEN 沿 sub 出边继续；并行 join 汇聚后
  恢复的 SCOPE 立即复位 TOKEN，避免被收束扫描误杀。
- 边界 timer 中断子流程（M4-2a3）：进入 subProcess 时注册其边界作业；触发即
  取消整段 scope（内部子树全部结束：execution ENDED、任务归档、作业删除、
  actinst 结算、join 登记摘除），token 改走边界事件出边。
- 约束（文档化差异）：任何容器内并行分支路径不嵌套并行网关（沿用 M1 约束）；
  subProcess 的 asyncAfter 不支持（asyncBefore 支持，语义 = 展开前异步窗口）；
  非中断式边界（cancelActivity=false）M4-2b4 起支持普通等待活动宿主，subProcess
  宿主不支持（中断式支持，见 M4-2a3）。

M4-2b 扩展（事件子流程 + 错误传播）：
- 事件子流程（triggeredByEvent=true）不参与 sequenceFlow，由内部事件 start
  触发：error start（中断式）/ timer start（中断+非中断）/ message start
  （解析保留，消息投递入口未实现前订阅即明确报错——文档化差异）。
- 订阅生命周期对齐 Camunda：宿主 scope（流程实例或 subProcess）激活时建立
  触发条件（error = 冒泡匹配；timer = 注册实例级 timer-event-start Job），
  宿主 scope 结束即失效。
- error endEvent 抛出错误：沿宿主 scope 链（内到外：所在容器 -> 上级 subProcess
  -> 流程实例）冒泡找匹配的 error 事件子流程；命中 -> 中断宿主 scope 其它执行
  （根容器=整实例、subProcess 容器=该 subProcess 内部），事件子流程成为宿主
  scope 下唯一活动，走完即宿主 scope 结束（根=实例完成 / subProcess=正常复活
  沿出边继续）；无命中 -> 等同 none end（仅当前路径结束，文档化差异对齐 Camunda
  error end event 默认语义）。
- 事件子流程 scope 建模：SCOPE execution 停驻在事件子流程节点（activity_id=
  事件子流程 id），与 embedded SubProcess 同一收束/容器推导路径，零表改动。
- 非中断式边界事件（cancelActivity=false，M4-2b4 落地）：宿主等待期内触发
  不取消宿主，spawn 并发线从边界事件出边走；主线与并发线全收束才算实例完成
  （root 到 end 不再等价实例完成——root 转 SCOPE 停驻等并发子树收束后收尾）。

时间来自可注入时钟 camunda.common.clock（测试拨时间无需真等待）。所有命令入口
持引擎级 RLock，JobExecutor 轮询线程与用户命令不会互踩（多进程部署锁不在范围）。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # Store 仅持久化模式实例化，纯内存模式不强依赖 sqlalchemy
    from camunda.persistence.store import ProcInstSnap, Store

from camunda.common import clock
from camunda.common.exceptions import (
    InvalidRequestException,
    NotFoundException,
    ProcessInstanceException,
)
from camunda.common.idgen import IdGenerator
from camunda.common.timers import (
    format_iso,
    next_trigger,
    parse_iso,
    parse_iso_repeat,
    parse_trigger_date,
)
from camunda.engine.behavior import select_exclusive_gateway_flow
from camunda.engine.expression import evaluate_condition, evaluate_expression
from camunda.model.bpmn import (
    BpmnModel,
    BoundaryEvent,
    EndEvent,
    ExclusiveGateway,
    BusinessRuleTask,
    FlowNode,
    IntermediateCatchEvent,
    IntermediateThrowEvent,
    MultiInstance,
    ParallelGateway,
    Process,
    SequenceFlow,
    ServiceTask,
    StartEvent,
    SubProcess,
    UserTask,
)
from camunda.model.execution import (
    ActivityInstance,
    Execution,
    ExecutionState,
    ProcessInstance,
    ProcessInstanceState,
)
from camunda.model.job import Job
from camunda.model.task import Task
from camunda.dmn.engine import DmnEngine
from camunda.model.dmn import DmnModel

# 到达事件：(token execution, 即将进入的节点)
_Arrival = Tuple[Execution, FlowNode]


@dataclass
class EventSubscription:
    """消息/信号事件订阅（M4-2d，纯内存派生态——不落库，恢复时重推导）。

    挂载点（node_kind）：
    - start：事件子流程 message/signal start，execution = 宿主 scope
      （activity_id = 订阅容器 subProcess id，None = 流程级）；
    - catch：IntermediateCatchEvent 停等 token，execution = 停等执行；
    - boundary：宿主停等活动上的 message/signal 边界，execution = 宿主 token。

    kind: "message" | "signal"；is_interrupting：boundary 用 cancel_activity、
    esc start 用 isInterrupting（catch 无中断概念恒 True 填充）。
    生命周期：scope 激活即注册；触发即消费（catch/boundary-中断/esc-中断式）
    或随宿主离开/杀灭/收束撤销；非中断式 boundary 与 esc-start 订阅常驻可再触发。
    """

    id: str
    kind: str  # "message" | "signal"
    event_name: str
    process_instance_id: str
    execution_id: str
    activity_id: Optional[str]  # esc 订阅容器 subProcess id（None = 流程级）
    node_id: str  # 事件节点 id（esc start / catch / boundary）
    node_kind: str  # "start" | "catch" | "boundary"
    is_interrupting: bool
    created: str

# 实现委托签名：callable(variables: dict) -> None | dict(merge 更新)
Delegate = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


def _now() -> str:
    """当前时间（定长 ISO，本地时区）。走可注入时钟，测试可拨快。"""
    return clock.now()


# M3：单条作业执行失败不阻塞整轮轮询（对齐 Camunda JobExecutor 行为）
logger = logging.getLogger(__name__)


class ProcessEngine:
    """流程引擎（M1 内存版 / M2 可选持久化 / M3 作业）。

    M2/M3 用法：
        from camunda.persistence.store import Store
        engine = ProcessEngine(store=Store("sqlite:///camunda.db"))   # 启用落库
        engine = ProcessEngine.from_database("sqlite:///camunda.db")  # 崩溃恢复
        engine.execute_due_jobs()      # 手动触发到期作业（JobExecutor 轮询也是调它）
        engine.create_job_query()      # 查看作业（待办/死信）

    未传 store 时行为与 M1 完全一致（纯内存，既有测试不破坏）。
    多进程部署抢锁不在 M3 范围（见 docs/ARCHITECTURE.md 风险章节）。
    """

    def __init__(self, store: Optional["Store"] = None) -> None:
        # key -> Process（同名重复部署视为新版本，覆盖并版本+1）
        self._definitions: Dict[str, Process] = {}
        self._definition_versions: Dict[str, int] = {}
        # M6：key -> 原始 BPMN XML（供 REST GET /process-definition/{key}/xml）
        self._definition_sources: Dict[str, Optional[str]] = {}
        self._instances: Dict[str, ProcessInstance] = {}
        self._tasks: Dict[str, Task] = {}
        # 实现注册表：serviceTask implementation_ref -> callable
        self._delegates: Dict[str, Delegate] = {}
        self._idgen = IdGenerator()
        # M2：持久化 store（None = 纯内存模式）
        self._store = store
        # M3：作业池（实例级 timer-catch/async + 定义级 timer-start）
        self._jobs: Dict[str, Job] = {}
        # M4-2d：消息/信号订阅池（插入序 = 注册序，事件订阅检索按序取最早）
        self._event_subs: Dict[str, EventSubscription] = {}
        # M5：DMN 决策引擎（部署不落库，对齐 delegate 注册先例）
        self._dmn = DmnEngine()
        # M6：部署记录（id/name/time/keys，供 REST GET /deployment 列举）
        self._deployments: List[Dict[str, Any]] = []
        # 命令级互斥：JobExecutor 轮询线程与用户命令入口共用
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 委托注册（对齐 Spring bean / JavaDelegate 注册语义）
    # ------------------------------------------------------------------
    def register_delegate(self, name: str, fn: Delegate) -> None:
        """注册 serviceTask 实现。fn(variables) 原地改或返回 dict 合并。"""
        with self._lock:
            if not callable(fn):
                raise InvalidRequestException(f"delegate {name!r} 必须可调用")
            self._delegates[name] = fn

    # ------------------------------------------------------------------
    # RepositoryService 语义
    # ------------------------------------------------------------------
    def deploy(self, model: BpmnModel, name: Optional[str] = None) -> List[str]:
        """部署 BpmnModel，返回部署成功的 process key 列表。

        重复部署同名 key：覆盖并版本 +1（对齐 ACT_RE_PROCDEF 多版本语义）。
        M2：启用 store 时同步写 ACT_RE_*（含原始 xml 供恢复重解析）。
        M3：带 timer start 的流程注册定义级 timer-start 作业（新版本覆盖旧版作业组）。
        """
        with self._lock:
            keys: List[str] = []
            for proc in model.processes:
                if not proc.is_executable:
                    continue
                self._definitions[proc.id] = proc
                self._definition_versions[proc.id] = (
                    self._definition_versions.get(proc.id, 0) + 1
                )
                self._definition_sources[proc.id] = model.source_xml
                keys.append(proc.id)
                # 新版本取代旧版本 -> 该 key 的旧 timer-start 作业组整体移除重建
                self._drop_definition_jobs(proc.id)
                for start in proc.start_events:
                    if start.timer is not None:
                        job = self._make_timer_start_job(proc.id, start)
                        self._jobs[job.id] = job
            if keys:
                # 落库时复用 store 生成的部署 id；纯内存用 idgen
                dep_id = (
                    self._store.save_deployment(model, _now())
                    if self._store is not None
                    else self._idgen.next_id()
                )
                if self._store is not None:
                    self._sync_timer_start_jobs()
                self._deployments.append(
                    {
                        "id": dep_id,
                        "name": name,
                        "time": _now(),
                        "source": model.source_name,
                        "process_keys": list(keys),
                        "decision_keys": [],
                    }
                )
            return keys

    def get_process_definition(self, key: str) -> Process:
        with self._lock:
            if key not in self._definitions:
                raise NotFoundException(f"未部署的流程定义: {key!r}")
            return self._definitions[key]

    def get_definition_version(self, key: str) -> int:
        return self._definition_versions.get(key, 0)

    # ------------------------------------------------------------------
    # DecisionService 语义（M5：DMN 决策）
    # ------------------------------------------------------------------
    def deploy_dmn(self, model: DmnModel, name: Optional[str] = None) -> List[str]:
        """部署 DmnModel，返回 decision key 列表（重复 key 版本 +1）。

        注意：DMN 部署不落库（文档化差异，对齐 delegate 注册不落库）——
        崩溃恢复后须重新 deploy_dmn，否则 businessRuleTask 求值报未部署。
        """
        with self._lock:
            keys = self._dmn.deploy(model)
            if keys:
                self._deployments.append(
                    {
                        "id": self._idgen.next_id(),
                        "name": name,
                        "time": _now(),
                        "source": model.source_name,
                        "process_keys": [],
                        "decision_keys": list(keys),
                    }
                )
            return keys

    def evaluate_decision(self, key: str, variables: Optional[Dict[str, Any]] = None) -> Any:
        """直接求值已部署决策（对齐 DecisionService.evaluateDecisionTable）。"""
        with self._lock:
            return self._dmn.evaluate_decision(key, variables)

    def get_decision_definition(self, key: str):
        """按 key 取已部署决策（未部署抛 NotFoundException）。"""
        with self._lock:
            return self._dmn.get_decision(key)

    def get_decision_version(self, key: str) -> int:
        return self._dmn.get_decision_version(key)

    # ------------------------------------------------------------------
    # RuntimeService 语义
    # ------------------------------------------------------------------
    def start_process_instance_by_key(
        self,
        process_key: str,
        variables: Optional[Dict[str, Any]] = None,
        business_key: Optional[str] = None,
    ) -> ProcessInstance:
        """按 key 启动实例：建树 -> startEvent token 入队 pump。

        定时启动流程（startEvent 带 timer）不可手动启动，对齐 Camunda 语义。
        """
        with self._lock:
            proc = self.get_process_definition(process_key)
            if not proc.start_events:
                raise ProcessInstanceException(f"流程 {process_key!r} 没有可启动的 startEvent")
            start = proc.start_events[0]  # M1：取第一个 startEvent
            if start.timer is not None:
                raise ProcessInstanceException(
                    f"流程 {process_key!r} 是定时启动流程（startEvent {start.id} 带 timer），不能手动启动"
                )
            if start.error_code is not None or start.message_name is not None or start.signal_name is not None:
                raise ProcessInstanceException(
                    f"流程 {process_key!r} 的 startEvent {start.id} 是事件 start"
                    "（error/message/signal）：流程级事件启动后续里程碑落地，不能手动启动"
                )
            return self._start_process(proc, variables, business_key, start)

    def _start_process(
        self,
        proc: Process,
        variables: Optional[Dict[str, Any]],
        business_key: Optional[str],
        start: Optional[StartEvent] = None,
    ) -> ProcessInstance:
        """内部启动：手动与 timer-start 共用。timer 触发时传入对应 startEvent。"""
        start = start or proc.start_events[0]
        pi = ProcessInstance(
            id=self._idgen.next_id(),
            process_definition_key=proc.id,
            business_key=business_key,
            variables=dict(variables or {}),
            start_time=_now(),
        )
        root = Execution(id=self._idgen.next_id(), process_instance_id=pi.id)
        pi.root_execution = root
        pi.executions[root.id] = root
        self._instances[pi.id] = pi

        self._pump(pi, [(root, start)])
        # M4-2b3：流程实例 scope 激活 -> 订阅根 Process 容器内 timer 事件子流程
        # （root 直通进入 sub 时同样成立——容器由注册点显式给出，不与 sub 订阅混淆）
        if not pi.is_completed:
            self._register_event_subprocess_timers(pi, root, proc, None)
            # M4-2d：流程级容器 message/signal esc start 常驻订阅
            self._register_event_subprocess_subscriptions(pi, root, proc, None)
        if self._store is not None:
            self._store.save_proc_inst(self._build_snap(pi))
        return pi

    def get_process_instance(self, instance_id: str) -> ProcessInstance:
        with self._lock:
            if instance_id not in self._instances:
                raise NotFoundException(f"流程实例不存在: {instance_id!r}")
            return self._instances[instance_id]

    def list_process_instances(self) -> List[ProcessInstance]:
        with self._lock:
            return list(self._instances.values())

    # ------------------------------------------------------------------
    # M6 补充：REST 需要的门面能力（定义列表 / 删除实例 / 任务认领）
    # ------------------------------------------------------------------
    def list_process_definitions(self) -> List[Dict[str, Any]]:
        """已部署流程定义列表（key / name / version，部署序）。"""
        with self._lock:
            return [
                {
                    "key": key,
                    "name": proc.name,
                    "version": self._definition_versions.get(key, 0),
                }
                for key, proc in self._definitions.items()
            ]

    def get_process_definition_xml(self, key: str) -> Optional[str]:
        """流程定义原始 XML（部署时未带 source_xml 则为 None）。"""
        with self._lock:
            if key not in self._definitions:
                raise NotFoundException(f"未部署的流程定义: {key!r}")
            return self._definition_sources.get(key)

    def list_deployments(self) -> List[Dict[str, Any]]:
        """部署记录列表（部署序，M6 供 REST GET /deployment 使用）。"""
        with self._lock:
            return list(self._deployments)

    def list_decision_definitions(self) -> List[Dict[str, Any]]:
        """已部署决策定义列表（key / name / version，部署序）。"""
        with self._lock:
            return self._dmn.list_decisions()

    def delete_process_instance(
        self, instance_id: str, reason: Optional[str] = None
    ) -> None:
        """删除流程实例（运行中亦可）：清内存态 + RU 行，历史保留。

        对齐 Camunda 默认语义：不传 skipHistory 时历史行保留（HI_PROCINST 置
        DELETED）。已结束实例同样可删（幂等清理运行时残渣，历史不动）。
        """
        with self._lock:
            pi = self._instances.get(instance_id)
            if pi is None:
                raise NotFoundException(f"流程实例不存在: {instance_id!r}")
            # 1) 该实例的任务全部下线（活跃任务不入历史归档，删实例非正常完成）
            for tid in [t.id for t in self._tasks.values()
                        if t.process_instance_id == instance_id]:
                self._tasks.pop(tid, None)
            # 2) 该实例的作业与事件订阅清理（定义级 timer-start 不挂实例，不动）
            for jid in [j.id for j in self._jobs.values()
                        if j.process_instance_id == instance_id]:
                self._jobs.pop(jid, None)
            for sid in [s.id for s in self._event_subs.values()
                        if s.process_instance_id == instance_id]:
                self._event_subs.pop(sid, None)
            # 3) 实例本体移除
            self._instances.pop(instance_id, None)
            if self._store is not None:
                self._store.delete_proc_inst(instance_id, _now())

    def get_task(self, task_id: str) -> Task:
        """按 id 取活跃任务（不存在抛 NotFoundException）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundException(f"任务不存在或已完成: {task_id!r}")
            return task

    def claim_task(self, task_id: str, user_id: str) -> Task:
        """认领任务：设置 assignee。已认领给他人时报错（Camunda 语义）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundException(f"任务不存在或已完成: {task_id!r}")
            if task.assignee is not None and task.assignee != user_id:
                raise InvalidRequestException(
                    f"任务 {task_id!r} 已指派给 {task.assignee!r}，不能由 {user_id!r} 认领"
                )
            task.assignee = user_id
            self._sync_instance(task.process_instance_id)
            return task

    def unclaim_task(self, task_id: str) -> Task:
        """取消认领：清空 assignee（任务回到候选组池）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundException(f"任务不存在或已完成: {task_id!r}")
            task.assignee = None
            self._sync_instance(task.process_instance_id)
            return task

    def set_assignee(self, task_id: str, user_id: Optional[str]) -> Task:
        """直接指派/清空 assignee（不做「已被他人认领」校验）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundException(f"任务不存在或已完成: {task_id!r}")
            task.assignee = user_id
            self._sync_instance(task.process_instance_id)
            return task

    def set_variable(self, instance_id: str, name: str, value: Any) -> None:
        """设置实例变量（M6：PUT /process-instance/{id}/variables/{name}）。

        变量为实例级（文档化差异，对齐 M4-2a 起的一贯语义），无作用域隔离。
        """
        with self._lock:
            pi = self._instances.get(instance_id)
            if pi is None:
                raise NotFoundException(f"流程实例不存在: {instance_id!r}")
            pi.variables[name] = value
            if self._store is not None:
                self._store.save_proc_inst(self._build_snap(pi))

    def _sync_instance(self, process_instance_id: str) -> None:
        """任务级变更后同步实例快照（assignee 需落库，否则崩溃恢复丢失）。"""
        if self._store is None:
            return
        pi = self._instances.get(process_instance_id)
        if pi is not None:
            self._store.save_proc_inst(self._build_snap(pi))

    # ------------------------------------------------------------------
    # M4-2d：消息关联 / 信号广播（RuntimeService correlateMessage 语义）
    # ------------------------------------------------------------------
    def correlate_message(
        self,
        name: str,
        process_instance_id: Optional[str] = None,
        variables: Optional[Dict[str, Any]] = None,
    ) -> None:
        """消息关联：把消息投递给一个等待中的 message 订阅并触发。

        点对点 1:1（对齐 Camunda message 语义）：命中的多个订阅里取注册序最早
        一个（Camunda 用 businessKey/processInstanceId 消歧，本引擎 v1 取最早
        ——文档化差异）。process_instance_id 限定后只在实例内匹配。

        可命中的订阅形态（M4-2d）：
        - IntermediateCatchEvent 停等 token（catch）；
        - 宿主停等活动上的 message 边界事件（boundary，中断式取消宿主 / 非中断
          spawn 并发线）；
        - 事件子流程 message start（宿主 scope 激活期常驻订阅，中断式接管 /
          非中断 spawn，可多次触发）。
        variables 随消息合并进目标实例变量表。无等待订阅 -> NotFoundException。
        """
        with self._lock:
            sub = self._find_event_subscription("message", name, process_instance_id)
            if sub is None:
                raise NotFoundException(
                    f"消息 {name!r} 没有等待中的订阅"
                    + (f"（流程实例 {process_instance_id!r} 内）" if process_instance_id else "")
                )
            pi = self._instances.get(sub.process_instance_id)
            if pi is None or pi.is_completed:
                # 防御：订阅指向已结束实例（清理钩子漏网时惰性回收）
                self._event_subs.pop(sub.id, None)
                raise ProcessInstanceException(
                    f"消息 {name!r} 命中的流程实例已结束: {sub.process_instance_id!r}"
                )
            if variables:
                pi.variables.update(variables)
            self._fire_subscription(pi, sub)

    def throw_signal(
        self,
        name: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> int:
        """信号广播：把信号投给当前全部等待中的 signal 订阅并触发。

        广播语义（对齐 Camunda signal）：命中所有订阅（跨实例、每实例内多个
        订阅同时触发）；无订阅命中则静默无效果（返回 0）。variables 合并到
        每个命中实例的变量表。返回触发的订阅数。
        """
        with self._lock:
            hit = [
                s
                for s in self._event_subs.values()
                if s.kind == "signal" and s.event_name == name
            ]
            for sub in hit:
                pi = self._instances.get(sub.process_instance_id)
                if pi is None or pi.is_completed:
                    self._event_subs.pop(sub.id, None)  # 惰性回收
                    continue
                if variables:
                    pi.variables.update(variables)
                self._fire_subscription(pi, sub)
            return len(hit)

    def _find_event_subscription(
        self,
        kind: str,
        name: str,
        process_instance_id: Optional[str] = None,
    ) -> Optional[EventSubscription]:
        """按（kind, name[, pi]）取注册序最早的等待订阅；过期订阅惰性剔除。"""
        for sub in list(self._event_subs.values()):
            if sub.kind != kind or sub.event_name != name:
                continue
            if (
                process_instance_id is not None
                and sub.process_instance_id != process_instance_id
            ):
                continue
            pi = self._instances.get(sub.process_instance_id)
            token = pi.executions.get(sub.execution_id) if pi is not None else None
            if (
                pi is None
                or pi.is_completed
                or token is None
                or token.state != ExecutionState.ACTIVE
            ):
                self._event_subs.pop(sub.id, None)  # 订阅已失效 -> 跳过并回收
                continue
            return sub
        return None

    def _fire_subscription(self, pi: ProcessInstance, sub: EventSubscription) -> None:
        """订阅触发统一入口：按挂载形态分派（stale 订阅惰性剔除）。

        - catch：结算停等 actinst -> token 沿出边推进；
        - boundary：中断式取消宿主（复用 timer 边界取消链）/ 非中断 spawn 并发线；
        - start：事件子流程接管（中断式 interrupt / 非中断直接 spawn）。
        """
        token = pi.executions.get(sub.execution_id)
        if token is None or token.state != ExecutionState.ACTIVE or pi.is_completed:
            self._event_subs.pop(sub.id, None)  # 惰性回收（清理钩子兜底）
            return
        if sub.node_kind == "catch":
            self._fire_event_catch(pi, token, sub)
            return
        if sub.node_kind == "boundary":
            self._fire_event_boundary(pi, token, sub)
            return
        if sub.node_kind == "start":
            self._fire_event_esc_start(pi, token, sub)
            return
        self._event_subs.pop(sub.id, None)  # 防御：未知挂载形态

    def _fire_event_catch(
        self, pi: ProcessInstance, token: Execution, sub: EventSubscription
    ) -> None:
        """message/signal 中间捕获触发：结算停等 -> 沿出边继续。"""
        proc = self._container_of(pi, token)
        node = proc.flow_nodes.get(sub.node_id)
        if (
            not isinstance(node, IntermediateCatchEvent)
            or token.activity_id != node.id
            or token.open_activity is None
            or token.open_activity.end_time is not None
        ):
            self._event_subs.pop(sub.id, None)  # token 已离开 -> 过期订阅丢弃
            return
        self._event_subs.pop(sub.id, None)  # 触发即消费
        self._close_activity(pi, token, node)
        arrivals: List[_Arrival] = []
        self._leave(pi, token, node, arrivals)
        self._pump(pi, arrivals)
        if self._store is not None:
            self._store.save_proc_inst(self._build_snap(pi))

    def _fire_event_boundary(
        self, pi: ProcessInstance, token: Execution, sub: EventSubscription
    ) -> None:
        """message/signal 边界触发：中断式取消宿主 / 非中断 spawn 并发线。"""
        proc = self._container_of(pi, token)
        boundary = proc.flow_nodes.get(sub.node_id)
        host = (
            proc.flow_nodes.get(boundary.attached_to)
            if isinstance(boundary, BoundaryEvent) and boundary.attached_to
            else None
        )
        if (
            not isinstance(boundary, BoundaryEvent)
            or host is None
            or token.activity_id != host.id
            or token.open_activity is None
            or token.open_activity.end_time is not None
        ):
            self._event_subs.pop(sub.id, None)  # 宿主已离开 -> 过期订阅丢弃
            return
        if boundary.cancel_activity:
            # 中断式：取消宿主（_cancel_host_activity 撤宿主全部边界订阅含本条），
            # token 改走边界事件出边（与 timer 边界中断路径同构）
            self._cancel_host_activity(pi, token, host)
            self._open_activity(pi, token, boundary)
            self._close_activity(pi, token, boundary)
            arrivals: List[_Arrival] = []
            self._leave(pi, token, boundary, arrivals)
            self._pump(pi, arrivals)
            if self._store is not None:
                self._store.save_proc_inst(self._build_snap(pi))
        else:
            # 非中断式：宿主保留，订阅常驻（可再次触发）；spawn 并发线
            self._spawn_non_interrupting_boundary(pi, token, boundary)

    def _fire_event_esc_start(
        self, pi: ProcessInstance, host: Execution, sub: EventSubscription
    ) -> None:
        """事件子流程 message/signal start 触发（中断式接管 / 非中断 spawn）。"""
        root_proc = self._definitions[pi.process_definition_key]
        # 容器由订阅自身携带：None = 根 Process；否则 = sub_id 对应 sub 的 inner
        # （一致性校验与 timer esc 触发同构：sub 级订阅仅在 host 仍停驻同一 sub）
        if sub.activity_id is None:
            container = root_proc
        else:
            if host.role != "SCOPE" or host.activity_id != sub.activity_id:
                self._event_subs.pop(sub.id, None)  # 宿主已离开订阅容器
                return
            outer = self._container_of(pi, host)
            parked = outer.flow_nodes.get(sub.activity_id)
            if not isinstance(parked, SubProcess):
                self._event_subs.pop(sub.id, None)
                return
            container = parked.process
        event_sub: Optional[SubProcess] = None
        start: Optional[StartEvent] = None
        for esc in container.flow_nodes.values():
            if not (isinstance(esc, SubProcess) and esc.triggered_by_event):
                continue
            inner = esc.process
            if inner is None:
                continue
            for st in inner.start_events:
                if st.id == sub.node_id:
                    event_sub, start = esc, st
                    break
        if event_sub is None or start is None:
            self._event_subs.pop(sub.id, None)  # 防御：订阅目标不存在
            return
        if start.is_interrupting:
            # 中断式：触发即消费（随后 _fire_esc_event 整容器订阅一并撤销）
            self._event_subs.pop(sub.id, None)
        # 非中断式：订阅常驻，可再次触发（每次关联/广播 spawn 一个新实例）
        self._fire_esc_event(pi, host, sub.activity_id, event_sub, start)

    def _fire_esc_event(
        self,
        pi: ProcessInstance,
        host: Execution,
        container_id: Optional[str],
        event_sub: SubProcess,
        start: StartEvent,
    ) -> None:
        """事件子流程 message/signal start 触发主体（中断语义与 timer esc 同构）。

        中断式：流程级 interrupt 整实例 / sub 级 kill scope 内部；随后撤订阅容器
        全部 esc 订阅（timer job + message/signal sub，宿主被接管同容器订阅失效）。
        非中断式：宿主保留，直接 spawn 事件子流程（message/signal start 订阅常驻
        可再次触发——与 timer 非中断单发的差异，文档化）。
        """
        if start.is_interrupting:
            if container_id is None:
                self._interrupt_instance(pi)  # 流程级：清 root 全部实例级状态
            else:
                self._kill_subprocess_scope(pi, host)
            self._drop_scope_event_jobs(pi, host, container_id)
            self._drop_scope_event_subscriptions(pi, host, container_id)
        arrivals = self._start_event_subprocess(pi, host, event_sub, start)
        self._pump(pi, arrivals)
        if self._store is not None:
            self._store.save_proc_inst(self._build_snap(pi))

    # ------------------------------------------------------------------
    # TaskService 语义
    # ------------------------------------------------------------------
    def create_task_query(self, process_instance_id: Optional[str] = None) -> List[Task]:
        """查询任务（M1：无分页，创建序）。"""
        with self._lock:
            tasks = list(self._tasks.values())
            if process_instance_id is not None:
                tasks = [t for t in tasks if t.process_instance_id == process_instance_id]
            return tasks

    def complete_task(
        self,
        task_id: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> None:
        """完成任务：合并变量 -> token 从 userTask 离开 -> pump。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundException(f"任务不存在或已完成: {task_id!r}")
            pi = self._instances[task.process_instance_id]
            if pi.is_completed:
                raise ProcessInstanceException(
                    f"流程实例 {pi.id} 已结束，任务 {task_id} 不可再完成"
                )
            token = pi.executions.get(task.execution_id)
            if token is None or token.state != ExecutionState.ACTIVE:
                raise ProcessInstanceException(f"任务 {task_id} 对应的执行已失效")

            if variables:
                pi.variables.update(variables)
            # 归档到已完成任务（HI_TASKINST 落库），并从活跃任务表移除
            task.end_time = _now()
            pi.completed_tasks.append(task)
            self._tasks.pop(task_id)

            proc = self._container_of(pi, token)
            node = proc.flow_nodes[task.task_definition_key]
            self._close_activity(pi, token, node)
            self._drop_boundary_jobs(pi, node)  # 宿主正常离开：边界 timer 作废
            # 多实例宿主（M4-2c）：userTask 是某个 MI 实例的行为载体 ->
            # 走实例完成路径（计数/条件/收束/续跑），不沿普通出边走。
            mi_scope = self._mi_scope_of(pi, token)
            if mi_scope is not None:
                # 实例完成路径会返回续跑/收束推进事件（如顺序 subProcess 宿主启动
                # 下一实例、容器收束沿宿主出边离开），在此统一 pump
                self._pump(pi, self._complete_mi_instance(pi, token, node, mi_scope))
                if self._store is not None:
                    self._store.save_proc_inst(self._build_snap(pi))
                return
            arrivals: List[_Arrival] = []
            self._leave(pi, token, node, arrivals)
            self._pump(pi, arrivals)
            if self._store is not None:
                self._store.save_proc_inst(self._build_snap(pi))

    # ------------------------------------------------------------------
    # 内部推进引擎
    # ------------------------------------------------------------------
    def _pump(self, pi: ProcessInstance, initial: List[_Arrival]) -> None:
        """事件队列推进：处理 token 到达直至队列空或实例结束。"""
        queue: Deque[_Arrival] = deque(initial)
        while queue and not pi.is_completed:
            token, node = queue.popleft()
            if token.state != ExecutionState.ACTIVE:
                continue  # 随兄弟汇聚/取消而失效，丢弃过期事件
            arrivals = self._handle_arrival(pi, token, node)
            queue.extend(arrivals)

    def _handle_arrival(
        self, pi: ProcessInstance, token: Execution, node: FlowNode
    ) -> List[_Arrival]:
        """token 进入节点：asyncBefore 拆分或按类型分派行为，返回后续到达事件。"""
        token.activity_id = node.id

        # 多实例宿主（M4-2c）：token 首次到达带 multiInstanceLoopCharacteristics
        # 的活动节点 -> 进入 MI 容器语义（parallel spawn / sequential 顺序循环）。
        # token.mi is None 守卫：sequential 续跑/parallel child 由 _start_mi_instance
        # 直接驱动行为，不再经本入口（防重复进入 MI 容器）。
        if node.multi_instance is not None and not token.is_mi_container:
            return self._enter_multi_instance(pi, token, node)
        # asyncBefore：节点行为执行拆成独立 job（async continuation 语义）
        if node.async_before:
            if self._schedule_async_before(pi, token, node):
                return []
        # asyncAfter 支持范围校验（M4-1：serviceTask / exclusiveGateway 行为后拆分；
        # 其余类型文档化差异不支持——userTask/并行网关等 asyncAfter 在 Camunda 有特定
        # 语义，M4-1 明确报错避免静默错位）
        if node.async_after and not isinstance(node, (ServiceTask, ExclusiveGateway)):
            raise InvalidRequestException(
                f"节点 {node.id!r} 声明 camunda:asyncAfter：M4-1 仅支持 serviceTask / "
                f"exclusiveGateway，{type(node).__name__} 的 asyncAfter 不支持（文档化差异）"
            )
        return self._dispatch_node(pi, token, node)

    def _dispatch_node(
        self, pi: ProcessInstance, token: Execution, node: FlowNode
    ) -> List[_Arrival]:
        """节点行为分派主体（async 作业执行时也直接调本方法，不再重复拆分）。"""
        proc = self._container_of(pi, token)
        arrivals: List[_Arrival] = []

        if isinstance(node, (StartEvent, EndEvent)):
            self._open_activity(pi, token, node)
            self._close_activity(pi, token, node)
            if isinstance(node, EndEvent):
                if node.error_code:
                    # 错误结束事件：token 结束 + 错误冒泡找事件子流程捕获
                    return self._throw_error(pi, token, node)
                if node.message_name is not None or node.signal_name is not None:
                    # M4-2d 消息/信号结束：token 结束同时实例内投递；若自身广播
                    # 触发的中断式订阅接管了实例（token 被杀）则不再收束
                    self._throw_event_in_instance(pi, token, node)
                    if token.state != ExecutionState.ACTIVE or pi.is_completed:
                        return []
                arrivals.extend(self._end_token(pi, token))
            else:
                self._leave(pi, token, node, arrivals)
            return arrivals

        if isinstance(node, SubProcess):
            return self._enter_subprocess(pi, token, node)

        if isinstance(node, IntermediateCatchEvent):
            return self._enter_event_catch(pi, token, node)

        if isinstance(node, IntermediateThrowEvent):
            # M4-2d：中间抛出事件（无等待窗口）：结算 actinst -> 实例内投递
            # message/signal -> token 沿出边继续（无出边则收束）。若投递触发
            # 的中断式订阅接管了实例（token 被杀）则不再继续
            self._open_activity(pi, token, node)
            self._close_activity(pi, token, node)
            if node.message_name is None and node.signal_name is None:
                raise InvalidRequestException(
                    f"中间抛出事件 {node.id!r} 未实现"
                    "（M4-2d 仅支持 message/signal throw）"
                )
            self._throw_event_in_instance(pi, token, node)
            if token.state != ExecutionState.ACTIVE or pi.is_completed:
                return []
            self._leave(pi, token, node, arrivals)
            return arrivals

        if isinstance(node, UserTask):
            return self._enter_user_task_wait(pi, token, node)

        if isinstance(node, ServiceTask):
            self._open_activity(pi, token, node)
            self._run_delegate(pi, token, node)
            self._close_activity(pi, token, node)
            if node.async_after:
                # 行为已完成（actinst 结算）：把「离开推进」拆成独立 async-after job
                self._schedule_async_after(pi, token, node)
                return arrivals
            self._leave(pi, token, node, arrivals)
            return arrivals

        if isinstance(node, BusinessRuleTask):
            # M5：同步求值 DMN 决策（无等待窗口）-> 结果写入 result_variable
            self._open_activity(pi, token, node)
            result = self._dmn.evaluate_decision(node.decision_ref, pi.variables)
            pi.variables[node.result_variable] = result
            self._close_activity(pi, token, node)
            if node.async_after:
                self._schedule_async_after(pi, token, node)
                return arrivals
            self._leave(pi, token, node, arrivals)
            return arrivals

        if isinstance(node, ExclusiveGateway):
            self._open_activity(pi, token, node)
            self._close_activity(pi, token, node)
            if node.async_after:
                # 网关无副作用：选路推迟到 async-after job（到期重新求值条件）
                self._schedule_async_after(pi, token, node)
                return arrivals
            chosen = select_exclusive_gateway_flow(
                node, self._outgoing(proc, node), pi.variables
            )
            self._take(pi, token, chosen, arrivals)
            return arrivals

        if isinstance(node, ParallelGateway):
            return self._handle_parallel_gateway(pi, token, node)

        raise InvalidRequestException(
            f"不支持的节点类型: {type(node).__name__} (id={node.id})"
        )

    # ------------------------------------------------------------------
    # M4-2a：embedded SubProcess 进入/收束（SCOPE 容器语义）
    # ------------------------------------------------------------------
    def _container_of(self, pi: ProcessInstance, e: Execution) -> Process:
        """execution 当前所在容器（内嵌子流程展开时随树推导，无需额外存储）。

        沿父链向上找第一个「停驻在 SubProcess 上的 SCOPE 祖先」——该祖先代表
        子流程执行体，取其 inner Process 即当前容器；无则根 Process。跨容器
        节点 id 不串扰（不同容器可有同名节点，归属只按树位置解析）。
        """
        cur: Optional[Execution] = e
        while cur.parent_id is not None:
            parent = pi.executions.get(cur.parent_id)
            if parent is None:
                break
            if parent.role == "SCOPE" and parent.activity_id:
                pnode = (
                    self._container_of(pi, parent)
                    .flow_nodes.get(parent.activity_id)
                )
                if isinstance(pnode, SubProcess):
                    # M4-2c3：仅「并行 MI 容器」停驻在宿主 subProcess 节点上但
                    # 不进子流程（进入的是其 child 实例）——它不是执行体，跳过
                    # 继续向外。其余 SCOPE@sub 都是该 sub 的执行体，取其 inner：
                    # 常规 embedded scope / 顺序 MI 容器（token 兼实例载体）/
                    # 并行实例载体（mi={"index"}，本身已进 sub 跑内部流转）。
                    if parent.is_mi_container and not parent.mi["sequential"]:
                        cur = parent
                        continue
                    return pnode.process
            cur = parent
        return self._definitions[pi.process_definition_key]

    def _enter_subprocess(
        self, pi: ProcessInstance, token: Execution, sub: SubProcess
    ) -> List[_Arrival]:
        """进入内嵌子流程：token 停驻为 SCOPE，spawn 内部子 token 从 startEvent 推进。

        - subProcess 活动实例跨整段内部执行 open（exit 时结算），对齐 Camunda
          HI_ACTINST 对 subProcess 的覆盖区间。
        - 变量作用域沿用实例级（文档化差异，无子作用域遮蔽）。
        - 边界 timer 注册：subProcess 是合法宿主，等待窗口 = 整段内部执行期
          （M4-2a3；非中断式 cancelActivity=false 仍拒绝）。
        - 事件子流程（triggeredByEvent）运行语义 M4-2b 落地，先明确报错。
        """
        if sub.triggered_by_event:
            raise InvalidRequestException(
                f"subProcess {sub.id!r} 是事件子流程（triggeredByEvent）："
                f"M4-2b 实现，当前不支持"
            )
        inner = sub.process
        if inner is None or not inner.start_events:
            raise ProcessInstanceException(
                f"subProcess {sub.id!r} 内部没有可启动的 startEvent"
            )
        start = inner.start_events[0]
        if start.timer is not None:
            raise InvalidRequestException(
                f"subProcess {sub.id!r} 内部 startEvent {start.id!r} 带 timer："
                f"内嵌子流程不支持定时启动（文档化差异）"
            )
        if start.error_code is not None or start.message_name is not None or start.signal_name is not None:
            raise InvalidRequestException(
                f"subProcess {sub.id!r} 内部 startEvent {start.id!r} 是事件 start"
                "（error/message/signal）：事件启动只属于事件子流程（文档化差异）"
            )
        self._open_activity(pi, token, sub)
        token.role = "SCOPE"
        # spawn 内部子 token：从 subProcess 内部 startEvent 开始推进
        child = Execution(
            id=self._idgen.next_id(),
            process_instance_id=pi.id,
            parent_id=token.id,
        )
        pi.executions[child.id] = child
        token.children.append(child)
        # 进入等待窗口：注册挂在 subProcess 上的边界 timer 作业
        self._register_boundary_jobs(pi, token, sub)
        # M4-2b3：subProcess scope 激活 -> 订阅该 sub 容器内事件子流程的 timer start
        self._register_event_subprocess_timers(pi, token, sub.process, sub.id)
        # M4-2d：该 sub 容器内 message/signal esc start 常驻订阅
        self._register_event_subprocess_subscriptions(pi, token, sub.process, sub.id)
        return [(child, start)]

    # ------------------------------------------------------------------
    # M4-2b：事件子流程（triggeredByEvent）与错误传播
    # ------------------------------------------------------------------
    def _throw_error(
        self, pi: ProcessInstance, token: Execution, node: EndEvent
    ) -> List[_Arrival]:
        """error endEvent 抛出：token 结束 + 错误沿宿主链冒泡找事件子流程捕获。

        命中 -> 中断宿主 scope 其它执行、事件子流程接管（其结束 = 宿主 scope
        结束：流程级 -> 实例完成；subProcess 级 -> 收束复活沿出边继续——错误由
        subProcess 自己消化）。未命中 -> 等同 none end（仅当前路径结束），对齐
        Camunda error end event 默认语义（日志 warning 留痕）。
        """
        code = node.error_code
        hit = self._find_error_catcher(pi, token, code)
        if hit is None:
            logger.warning(
                "流程实例 %s: error endEvent %s 抛出错误 %r 无事件子流程捕获，等同 none end",
                pi.id, node.id, code,
            )
            return self._end_token(pi, token)
        host, container, event_sub, start = hit
        if token.is_root or pi.root_execution is token:
            # 根执行到达 error end：实例整体被事件子流程接管。root 不结束——它
            # 转为事件子流程宿主载体（活动/子树清空由 _interrupt_instance 完成，
            # 事件子流程收束后 collapse 收尾 -> 实例完成）
            self._interrupt_instance(pi)
        else:
            # 子树内抛错：错误路径结束摘树，再中断宿主 scope 的其它执行
            token.state = ExecutionState.ENDED
            self._detach_from_parent(pi, token)
            if container is self._definitions[pi.process_definition_key]:
                self._interrupt_instance(pi)
            else:
                # subProcess（或嵌套事件子流程）级捕获：中断该容器 scope 内部，
                # 宿主本体保留（事件子流程走完后由收束链复活/上移）
                self._kill_subprocess_scope(pi, host)
                # 宿主被事件子流程接管：其容器内 timer 事件子流程订阅一并失效
                # （只撤被接管 sub 容器的订阅——host 若为 root 兼任，流程级订阅保留）
                self._drop_scope_event_jobs(pi, host, host.activity_id)
                # M4-2d：同容器 message/signal esc 订阅一并失效
                self._drop_scope_event_subscriptions(pi, host, host.activity_id)
        return self._start_event_subprocess(pi, host, event_sub, start)

    def _throw_event_in_instance(
        self, pi: ProcessInstance, token: Execution, node: FlowNode
    ) -> None:
        """实例内 message/signal throw（M4-2d：IntermediateThrowEvent / EndEvent）。

        - message：1:1 就近关联——本实例内匹配订阅取注册序最早（文档化差异：
          Camunda 沿 scope 链向外找最近命中；本引擎统一按注册序）。未命中 ->
          静默丢弃（对齐 Camunda throw message 无等待订阅即丢失，等同 none end）。
        - signal：本实例内广播全部匹配订阅（文档化差异：跨实例广播由公共 API
          throw_signal 提供，throw 事件本身只作用于本实例）。
        触发可能接管实例（中断式订阅杀死本 token）——调用方检查 token 存活性。
        """
        kind = "message" if node.message_name is not None else "signal"
        name = node.message_name or node.signal_name
        if kind == "message":
            sub = self._find_event_subscription("message", name, pi.id)
            if sub is None:
                logger.warning(
                    "流程实例 %s: throw message %r 无等待订阅，消息丢弃（token 继续流转）",
                    pi.id, name,
                )
                return
            self._fire_subscription(pi, sub)
            return
        for sub in [
            s
            for s in self._event_subs.values()
            if s.kind == "signal"
            and s.event_name == name
            and s.process_instance_id == pi.id
        ]:
            self._fire_subscription(pi, sub)

    def _find_error_catcher(
        self, pi: ProcessInstance, e: Execution, code: str
    ) -> Optional[Tuple[Execution, "Process", SubProcess, StartEvent]]:
        """沿宿主 scope 链（内到外）找能捕获错误 code 的事件子流程。

        返回 (host_execution, container, event_sub, start)：
        - host_execution：root 或停驻 SubProcess 的 SCOPE（事件子流程的宿主）
        - container：命中声明所在的容器 Process（根 Process = 流程级，中断整个
          实例；某 subProcess 的 inner = subProcess 级，中断该 scope 内部）
        - event_sub / start：命中的事件子流程与其 error start
        无命中返回 None（调用方按 none end 语义处理）。
        """
        root_proc = self._definitions[pi.process_definition_key]
        # 宿主链：从 e 所在容器逐层向外到根容器（先查内层声明，再冒到外层）
        chain: List[Tuple[Execution, Process]] = []
        cur = e
        while cur is not None and not (cur.is_root or pi.root_execution is cur):
            parent = pi.executions.get(cur.parent_id) if cur.parent_id else None
            if parent is None:
                break
            if parent.role == "SCOPE" and parent.activity_id:
                pnode = (
                    self._container_of(pi, parent).flow_nodes.get(parent.activity_id)
                )
                if isinstance(pnode, SubProcess):
                    chain.append((parent, pnode.process))
            cur = parent
        chain.append((pi.root_execution, root_proc))
        for host, container in chain:
            for sub in container.flow_nodes.values():
                if not (isinstance(sub, SubProcess) and sub.triggered_by_event):
                    continue
                inner = sub.process
                if inner is None:
                    continue
                for st in inner.start_events:
                    if st.error_code == code and st.is_interrupting:
                        return host, container, sub, st
        return None

    def _start_event_subprocess(
        self,
        pi: ProcessInstance,
        host: Execution,
        event_sub: SubProcess,
        start: StartEvent,
    ) -> List[_Arrival]:
        """在宿主 scope 下启动事件子流程实例（中断由调用方先行完成）。

        - 事件子流程 scope：role=SCOPE、activity_id=事件子流程 id、挂 host 下
          ——与 embedded SubProcess 共用容器推导/收束路径（零表改动）。事件子
          流程 actinst 跨整段执行 open，收束时由 collapse 的 SubProcess 分支结算。
        - 内部子 token 从匹配的 startEvent 推进（start 的 event 槽仅用于触发，
          运行语义 = 普通 startEvent 沿出边走）。
        - message/signal start（M4-2d）：与 timer/error start 同型——订阅触发
          由调用方（correlate_message / throw_signal）完成中断语义后进入。
        """
        inner = event_sub.process
        if inner is None:
            raise ProcessInstanceException(
                f"事件子流程 {event_sub.id!r} 内部容器缺失"
            )
        # message/signal start（M4-2d）：仅经由 _fire_esc_event 进入（correlate/
        # throw_signal 触发路径已完成中断语义）；timer/error 路径按事件槽匹配，
        # 不会命中 message/signal start，无需额外守卫。
        scope = Execution(
            id=self._idgen.next_id(),
            process_instance_id=pi.id,
            parent_id=host.id,
            role="SCOPE",
            activity_id=event_sub.id,
        )
        pi.executions[scope.id] = scope
        host.children.append(scope)
        child = Execution(
            id=self._idgen.next_id(),
            process_instance_id=pi.id,
            parent_id=scope.id,
        )
        pi.executions[child.id] = child
        scope.children.append(child)
        self._open_activity(pi, scope, event_sub)
        # M4-2b3：事件子流程 scope 自身激活 -> 订阅其内部嵌套事件子流程的 timer
        # start（宿主容器 = 本事件子流程的 inner）
        self._register_event_subprocess_timers(pi, scope, inner, event_sub.id)
        # M4-2d：事件子流程 scope 自身激活 -> 订阅其内部嵌套事件子流程的
        # message/signal start（宿主容器 = 本事件子流程的 inner）
        self._register_event_subprocess_subscriptions(pi, scope, inner, event_sub.id)
        return [(child, start)]

    def _interrupt_instance(self, pi: ProcessInstance) -> None:
        """流程级中断：结束实例内全部执行，root 保留为事件子流程宿主载体。

        结算 root 自身活动/任务/作业，杀全部子树（含 join 登记摘除、任务归档），
        清空 root.activity_id（事件子流程 scope 挂其下；收束后 collapse 收尾）。
        """
        now = _now()
        root = pi.root_execution
        if root is None:
            return
        if root.open_activity is not None and root.open_activity.end_time is None:
            root.open_activity.end_time = now
        root.open_activity = None
        for t in [
            t
            for t in self._tasks.values()
            if t.process_instance_id == pi.id and t.execution_id == root.id
        ]:
            self._tasks.pop(t.id)
            t.end_time = now
            pi.completed_tasks.append(t)
        for j in [
            j
            for j in self._jobs.values()
            if j.process_instance_id == pi.id and j.execution_id == root.id
        ]:
            self._jobs.pop(j.id)
        self._kill_subprocess_scope(pi, root)  # 子树全杀（任务归档/作业删除/join 摘除）
        # M4-2d：root 自身承载的订阅（流程级/message/signal esc、边界）一并作废
        # ——中断式触发方已撤容器订阅，此处兜底剩余（对齐 timer 全撤语义）
        self._drop_subscriptions_for_execution(pi, root.id)
        pi.join_arrivals.clear()  # 兜底清残留登记（kill 已逐棵摘除）
        root.activity_id = None
        root.role = "TOKEN"  # 复位：root 作为实例级事件子流程的宿主载体

    def _register_event_subprocess_timers(
        self,
        pi: ProcessInstance,
        host_scope: Execution,
        container: "Process",
        sub_id: Optional[str],
    ) -> None:
        """宿主 scope 激活订阅：容器内事件子流程的 timer start 注册实例级作业。

        container/sub_id 由调用点显式给出（流程实例启动 -> 根 Process/None；
        进入 embedded subProcess -> 该 sub 的 inner/sub.id），不反推——root 兼任
        sub 容器时（root 停驻 sub）两处订阅必须落在各自容器上、互不混淆。

        订阅生命周期对齐 Camunda 文档：scope（流程实例或 subProcess）创建即订阅、
        scope 结束或触发即撤销（_drop_scope_event_jobs）。每次激活单发：timer
        事件子流程不支持 cycle（文档化差异），非中断式 timer 同样只触发一次。
        """
        for sub in container.flow_nodes.values():
            if not (isinstance(sub, SubProcess) and sub.triggered_by_event):
                continue
            inner = sub.process
            if inner is None:
                continue
            for st in inner.start_events:
                if st.timer is None:
                    continue
                # 幂等：重复激活（如顺序 MI 宿主续跑再进同一 sub）不重复注册
                dup = any(
                    j.job_type == "timer-event-start"
                    and j.process_instance_id == pi.id
                    and j.execution_id == host_scope.id
                    and j.activity_id == sub_id
                    and j.node_id == st.id
                    for j in self._jobs.values()
                )
                if dup:
                    continue
                self._register_timer_event_start_job(pi, host_scope, sub, st, sub_id)

    def _register_timer_event_start_job(
        self,
        pi: ProcessInstance,
        host_scope: Execution,
        event_sub: SubProcess,
        start: StartEvent,
        sub_id: Optional[str],
    ) -> None:
        """为事件子流程的 timer start 注册一条 timer-event-start 作业（订阅）。"""
        timer = start.timer
        if timer.kind == "cycle":
            raise InvalidRequestException(
                f"事件子流程 {event_sub.id!r} 的 timer start {start.id!r} 不支持 "
                f"timeCycle（订阅单发，文档化差异）"
            )
        now = _now()
        if timer.kind == "duration":
            duedate = format_iso(
                parse_iso(now) + timedelta(seconds=timer.delay_seconds or 0)
            )
        else:  # date：绝对时间点
            duedate = parse_trigger_date(timer.value)
        job = Job(
            id=self._idgen.next_id(),
            job_type="timer-event-start",
            duedate=duedate,
            created=now,
            process_instance_id=pi.id,
            execution_id=host_scope.id,  # 宿主 scope；容器/事件子流程到期反查
            node_id=start.id,  # 事件子流程 startEvent id
            activity_id=sub_id,  # 订阅容器 subProcess id（None = 流程级）
        )
        self._jobs[job.id] = job

    def _drop_scope_event_jobs(
        self,
        pi: ProcessInstance,
        host_scope: Execution,
        sub_id: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """宿主 scope 事件子流程订阅撤销：按 (execution, 容器[, start]) 精确删除。

        sub_id=None 只撤流程级（根 Process 容器）订阅；传 sub_id 只撤该 subProcess
        容器上的订阅——root 兼任 sub 容器时两套订阅可独立撤销，互不误伤。
        node_id 给定则只撤该 timer start 的订阅（非中断式单发消费）。
        """
        stale = [
            jid
            for jid, j in self._jobs.items()
            if j.process_instance_id == pi.id
            and j.job_type == "timer-event-start"
            and j.execution_id == host_scope.id
            and j.activity_id == sub_id
            and (node_id is None or j.node_id == node_id)
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    def _register_event_subprocess_subscriptions(
        self,
        pi: ProcessInstance,
        host_scope: Execution,
        container: "Process",
        sub_id: Optional[str],
    ) -> None:
        """宿主 scope 激活订阅：容器内事件子流程的 message/signal start 注册订阅。

        与 timer start（实例级 job）平行；message/signal 是无限期等待 + 外部
        关联/广播触发 -> 常驻订阅表（_event_subs），宿主 scope 生命周期内有效：
        - 中断式 start：触发时撤容器全部 esc 订阅（宿主被接管）；
        - 非中断式 start：常驻可多次触发（每次触发 spawn 一个新实例）。
        幂等：同（宿主, 容器, start）重复激活（如顺序 MI 续跑再进同一 sub）
        不重复注册。container/sub_id 由调用点显式给出（同 timer 注册约定）。
        """
        for sub in container.flow_nodes.values():
            if not (isinstance(sub, SubProcess) and sub.triggered_by_event):
                continue
            inner = sub.process
            if inner is None:
                continue
            for st in inner.start_events:
                if st.message_name is None and st.signal_name is None:
                    continue
                kind = "message" if st.message_name is not None else "signal"
                name = st.message_name or st.signal_name
                dup = any(
                    s.kind == kind
                    and s.event_name == name
                    and s.execution_id == host_scope.id
                    and s.activity_id == sub_id
                    and s.node_id == st.id
                    for s in self._event_subs.values()
                )
                if dup:
                    continue  # 幂等：重复激活不重复注册
                s = EventSubscription(
                    id=self._idgen.next_id(),
                    kind=kind,
                    event_name=name,
                    process_instance_id=pi.id,
                    execution_id=host_scope.id,
                    activity_id=sub_id,
                    node_id=st.id,
                    node_kind="start",
                    is_interrupting=st.is_interrupting,
                    created=_now(),
                )
                self._event_subs[s.id] = s

    def _drop_scope_event_subscriptions(
        self,
        pi: ProcessInstance,
        host_scope: Execution,
        sub_id: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """宿主 scope 事件子流程消息/信号订阅撤销（按 execution + 容器精确删）。

        sub_id=None 只撤流程级订阅；传 sub_id 只撤该 subProcess 容器上的订阅。
        node_id 给定则只撤该 start 的订阅（触发消费用）。
        """
        stale = [
            sid
            for sid, s in self._event_subs.items()
            if s.process_instance_id == pi.id
            and s.node_kind == "start"
            and s.execution_id == host_scope.id
            and s.activity_id == sub_id
            and (node_id is None or s.node_id == node_id)
        ]
        for sid in stale:
            self._event_subs.pop(sid, None)

    def _drop_subscriptions_for_execution(
        self, pi: ProcessInstance, execution_id: str
    ) -> None:
        """撤销挂在某 execution 上的全部订阅（catch 停等 / 宿主等待 / esc 宿主）。

        供杀灭/取消路径使用（kill 树逐节点调用；宿主离开由 _drop_boundary_jobs
        专项撤销）。
        """
        stale = [
            sid
            for sid, s in self._event_subs.items()
            if s.process_instance_id == pi.id and s.execution_id == execution_id
        ]
        for sid in stale:
            self._event_subs.pop(sid, None)

    def _fire_timer_event_start(self, job: Job) -> None:
        """timer 事件子流程到期：宿主 scope 仍激活则触发（中断/非中断随 start）。

        触发即消费订阅：中断式撤该容器全部订阅（宿主被接管/取消）；非中断式只撤
        当前 timer start 的订阅（单发，同容器其它 esc 订阅保留）。宿主已失效
        （结束/离开停驻/被其它路径接管）-> 过期作业直接丢弃。
        """
        pi = self._instances.get(job.process_instance_id)
        host = pi.executions.get(job.execution_id) if pi is not None else None
        if (
            pi is None
            or pi.is_completed
            or host is None
            or host.state != ExecutionState.ACTIVE
        ):
            self._jobs.pop(job.id, None)
            return
        # 容器由订阅自身携带：None = 根 Process；否则 = sub_id 对应 sub 的 inner。
        # 一致性校验：sub 级订阅仅在 host 仍以 SCOPE 停驻同一 sub 时有效
        root_proc = self._definitions[pi.process_definition_key]
        if job.activity_id is None:
            container = root_proc
        else:
            if host.role != "SCOPE" or host.activity_id != job.activity_id:
                self._jobs.pop(job.id, None)  # 宿主已离开订阅容器（收束/被接管）
                return
            outer = self._container_of(pi, host)
            parked = outer.flow_nodes.get(job.activity_id)
            if not isinstance(parked, SubProcess):
                self._jobs.pop(job.id, None)
                return
            container = parked.process
        event_sub: Optional[SubProcess] = None
        start: Optional[StartEvent] = None
        for sub in container.flow_nodes.values():
            if not (isinstance(sub, SubProcess) and sub.triggered_by_event):
                continue
            inner = sub.process
            if inner is None:
                continue
            for st in inner.start_events:
                if st.id == job.node_id and st.timer is not None:
                    event_sub, start = sub, st
                    break
        if event_sub is None or start is None:
            self._jobs.pop(job.id, None)  # 防御：订阅目标已不存在（正常不会发生）
            return
        # 触发：中断式先取消宿主 scope 其它执行，再 spawn 事件子流程
        if start.is_interrupting:
            if job.activity_id is None:
                self._interrupt_instance(pi)  # 流程级：清 root 全部实例级作业
            else:
                self._kill_subprocess_scope(pi, host)
        # 订阅消费：中断式撤容器全部（宿主被取消，同容器订阅一并失效）；
        # 非中断式只撤当前 timer start（单发），同容器其它 esc 订阅保留
        if start.is_interrupting:
            self._drop_scope_event_jobs(pi, host, job.activity_id)
            # M4-2d：同容器 message/signal esc 订阅一并失效（中断语义与 timer 一致）
            self._drop_scope_event_subscriptions(pi, host, job.activity_id)
        else:
            self._drop_scope_event_jobs(pi, host, job.activity_id, job.node_id)
        arrivals = self._start_event_subprocess(pi, host, event_sub, start)
        self._pump(pi, arrivals)

    # ------------------------------------------------------------------
    # M3：timer catch 停等 / async 拆分
    # ------------------------------------------------------------------
    def _enter_event_catch(
        self, pi: ProcessInstance, token: Execution, node: IntermediateCatchEvent
    ) -> List[_Arrival]:
        """token 到达中间捕获事件：timer -> 注册 job 停等（M3）；message/signal
        -> 注册事件订阅停等（M4-2d），correlate_message / throw_signal 触发。"""
        if node.timer is not None:
            return self._handle_timer_catch(pi, token, node)
        if node.message_name is None and node.signal_name is None:
            raise InvalidRequestException(
                f"中间捕获事件 {node.id!r} 未实现（支持 timer/message/signal 事件定义）"
            )
        self._open_activity(pi, token, node)
        sub = EventSubscription(
            id=self._idgen.next_id(),
            kind="message" if node.message_name is not None else "signal",
            event_name=node.message_name or node.signal_name,
            process_instance_id=pi.id,
            execution_id=token.id,
            activity_id=None,
            node_id=node.id,
            node_kind="catch",
            is_interrupting=True,  # catch 无中断概念（触发即 token 续走）
            created=_now(),
        )
        self._event_subs[sub.id] = sub
        return []  # 停等外部触发

    def _handle_timer_catch(
        self, pi: ProcessInstance, token: Execution, node: IntermediateCatchEvent
    ) -> List[_Arrival]:
        """token 到达 timer 中间捕获事件：open actinst + 注册 timer-catch job，停等。"""
        timer = node.timer
        if timer is None:
            raise InvalidRequestException(
                f"中间捕获事件 {node.id!r} 未实现（M3 仅支持 timerEventDefinition）"
            )
        if timer.kind == "cycle":
            raise InvalidRequestException(
                f"timerCycle 仅用于 timer start；catch 事件 {node.id!r} 请用 timeDuration/timeDate"
            )
        self._open_activity(pi, token, node)
        now = _now()
        if timer.kind == "duration":
            duedate = format_iso(parse_iso(now) + timedelta(seconds=timer.delay_seconds or 0))
        else:  # date：绝对时间点（归一化到本地时区定长 ISO）
            duedate = parse_trigger_date(timer.value)
        job = Job(
            id=self._idgen.next_id(),
            job_type="timer-catch",
            duedate=duedate,
            created=now,
            process_instance_id=pi.id,
            execution_id=token.id,
            node_id=node.id,
        )
        self._jobs[job.id] = job
        return []  # 停等 job 到期

    def _schedule_async_before(self, pi: ProcessInstance, token: Execution, node: FlowNode) -> bool:
        """asyncBefore 拆分：open actinst + 立即可执行的 async-continuation job。

        返回 True = 已拆分停等（调用方应停止本轮推进）。actinst 保持 open，
        待 async job 执行行为时由 _open_activity 复用、行为完成后 close。
        asyncBefore 使节点获得等待窗口 -> 同步在此注册边界 timer（若宿主挂了
        边界事件；行为执行完成离开宿主时由调用方撤销）。
        """
        self._open_activity(pi, token, node)
        now = _now()
        job = Job(
            id=self._idgen.next_id(),
            job_type="async-continuation",
            duedate=now,  # 立即到期（async continuation 无额外延迟）
            created=now,
            process_instance_id=pi.id,
            execution_id=token.id,
            node_id=node.id,
        )
        self._jobs[job.id] = job
        self._register_boundary_jobs(pi, token, node)
        return True

    def _schedule_async_after(self, pi: ProcessInstance, token: Execution, node: FlowNode) -> None:
        """asyncAfter 拆分：节点行为已完成（actinst 结算），离开推进拆成独立 job。

        Camunda asyncAfter 语义：行为执行与 token 沿出边流转之间插入异步作业。
        serviceTask：行为（delegate）在拆分前已执行，job 到期只做离开；XOR：网关
        无副作用，选路本身推迟到 job 到期（此时重新求值出边条件）。asyncBefore +
        asyncAfter 链式：行为在 async-continuation job 中执行，完成后同样拆本 job。
        """
        now = _now()
        job = Job(
            id=self._idgen.next_id(),
            job_type="async-after",
            duedate=now,  # 立即到期（async continuation 无额外延迟）
            created=now,
            process_instance_id=pi.id,
            execution_id=token.id,
            node_id=node.id,
        )
        self._jobs[job.id] = job

    # ------------------------------------------------------------------
    # M4-1：timer 边界事件（中断式 interrupting；宿主 = 有等待点的活动）
    # ------------------------------------------------------------------
    def _register_boundary_jobs(
        self, pi: ProcessInstance, token: Execution, host: FlowNode
    ) -> None:
        """token 停等宿主活动（userTask / asyncBefore 拆分）时注册边界 timer 作业。

        作业语义：宿主等待期内到点触发——中断式（cancelActivity=true）取消宿主、
        token 改走边界事件出边；非中断式（cancelActivity=false，M4-2b4）不取消
        宿主，spawn 并发线从边界事件出边推进（宿主与并发线全收束实例才结束）。
        宿主正常离开（complete / async 行为完成）时由调用方 _drop_boundary_jobs
        撤销 —— 边界事件只对「仍在等待的宿主活动」有效。

        支持范围（文档化差异，见 docs/ARCHITECTURE.md）：
        - 中断式：userTask / asyncBefore 节点（M4-1）、subProcess（M4-2a3）
        - 非中断式（cancelActivity=false）：userTask / asyncBefore 等待活动宿主
          （M4-2b4）；subProcess 宿主 + 非中断仍明确报错（并发线需脱离 sub 容器
          身份挂父 scope，root 兼任载体时存在容器推导歧义，暂缓——文档化差异）
        - 事件变体：timer（M4-1，仅 timeDuration / timeDate；timeCycle 拒绝）、
          message / signal（M4-2d——注册为常驻订阅，触发后中断式随宿主撤销、
          非中断式保留可再触发；timer 系走 job 单发）
        - 宿主必须是有等待点的活动/容器：userTask / asyncBefore 节点
          （M4-1）、subProcess（M4-2a，等待窗口 = 整段内部执行）。同步节点
          （无 asyncBefore 的 serviceTask 等）没有等待窗口，本方法不会被调用
          —— 对齐 Camunda：同步活动在单命令内完成，边界事件无法插入中断。
        """
        proc = self._container_of(pi, token)
        existing = {
            j.node_id
            for j in self._jobs.values()
            if j.process_instance_id == pi.id and j.job_type == "timer-boundary"
        }
        existing_subs = {
            s.node_id
            for s in self._event_subs.values()
            if s.process_instance_id == pi.id
            and s.node_kind == "boundary"
            and s.execution_id == token.id
        }
        now = _now()
        for bid in host.boundary_events:
            boundary = proc.flow_nodes[bid]
            if not isinstance(boundary, BoundaryEvent):
                continue
            if not boundary.cancel_activity and isinstance(host, SubProcess):
                raise InvalidRequestException(
                    f"boundaryEvent {boundary.id!r} 声明 cancelActivity=false（非中断式）"
                    f"且宿主 {host.id!r} 是 subProcess：M4-2b4 支持普通等待活动宿主"
                    "（userTask / asyncBefore），subProcess 宿主非中断式边界暂不支持"
                    "（文档化差异）"
                )
            timer = boundary.timer
            if boundary.id in existing or boundary.id in existing_subs:
                continue  # 幂等：asyncBefore 拆分已注册，行为续跑不重复注册
            if timer is not None:
                if timer.kind == "cycle":
                    raise InvalidRequestException(
                        f"boundaryEvent {boundary.id!r} 不支持 timerCycle，请用 timeDuration/timeDate"
                    )
                if timer.kind == "duration":
                    duedate = format_iso(
                        parse_iso(now) + timedelta(seconds=timer.delay_seconds or 0)
                    )
                else:  # date：绝对时间点
                    duedate = parse_trigger_date(timer.value)
                job = Job(
                    id=self._idgen.next_id(),
                    job_type="timer-boundary",
                    duedate=duedate,
                    created=now,
                    process_instance_id=pi.id,
                    execution_id=token.id,
                    node_id=boundary.id,  # 边界事件 id；宿主经 attached_to 反查
                )
                self._jobs[job.id] = job
                continue
            if boundary.message_name is not None or boundary.signal_name is not None:
                # M4-2d：message/signal 边界 -> 常驻订阅（触发由关联/广播入口驱动）
                s = EventSubscription(
                    id=self._idgen.next_id(),
                    kind="message" if boundary.message_name is not None else "signal",
                    event_name=boundary.message_name or boundary.signal_name,
                    process_instance_id=pi.id,
                    execution_id=token.id,
                    activity_id=None,
                    node_id=boundary.id,
                    node_kind="boundary",
                    is_interrupting=boundary.cancel_activity,
                    created=now,
                )
                self._event_subs[s.id] = s
                continue
            raise InvalidRequestException(
                f"boundaryEvent {boundary.id!r} 未实现（支持 timer/message/signal 事件定义）"
            )

    def _drop_boundary_jobs(self, pi: ProcessInstance, host: FlowNode) -> None:
        """宿主活动正常离开/被取消：删除其全部边界 timer 作业与消息/信号订阅。

        M4-2a：宿主对象直接传入（调用方都持有），无需按 id 反查容器。
        M4-2d：边界 message/signal 订阅同窗同步撤销（宿主不再等待即失效）。
        """
        bound = set(host.boundary_events)
        if not bound:
            return
        stale = [
            jid
            for jid, j in self._jobs.items()
            if j.process_instance_id == pi.id
            and j.job_type == "timer-boundary"
            and j.node_id in bound
        ]
        for jid in stale:
            self._jobs.pop(jid, None)
        stale_subs = [
            sid
            for sid, s in self._event_subs.items()
            if s.process_instance_id == pi.id
            and s.node_kind == "boundary"
            and s.node_id in bound
        ]
        for sid in stale_subs:
            self._event_subs.pop(sid, None)

    def _cancel_host_activity(
        self, pi: ProcessInstance, token: Execution, host: FlowNode
    ) -> None:
        """中断式边界触发：取消宿主活动。

        宿主为普通活动（userTask / asyncBefore 节点）：删待办任务（归档留历史）
        -> 结算 actinst -> 撤销未执行 async 行为 -> 删边界 timer 作业。
        宿主为 subProcess（M4-2a3）：整段 scope 取消 = 先结束内部全部活跃子树
        （_kill_subprocess_scope），再结算 subProcess actinst 并清理其作业。
        """
        now = _now()
        if isinstance(host, SubProcess):
            self._kill_subprocess_scope(pi, token)
        else:
            # 宿主 userTask 的待办任务归档（end_time 结算 -> HI_TASKINST 留痕）
            for tid in [
                t.id
                for t in self._tasks.values()
                if t.process_instance_id == pi.id
                and t.execution_id == token.id
                and t.task_definition_key == host.id
            ]:
                task = self._tasks.pop(tid)
                task.end_time = now
                pi.completed_tasks.append(task)
        # 2) 结算宿主活动实例（中断 = 宿主活动结束；subProcess actinst 亦在此结算）
        self._close_activity(pi, token, host)
        # 3) 撤销宿主未执行的 asyncBefore 行为（拆分后行为还没跑，作废）
        for jid in [
            j.id
            for j in self._jobs.values()
            if j.process_instance_id == pi.id
            and j.job_type == "async-continuation"
            and j.execution_id == token.id
            and j.node_id == host.id
        ]:
            del self._jobs[jid]
        # 4) 宿主全部边界 timer 作业/消息信号订阅失效（含当前触发者自身）
        self._drop_boundary_jobs(pi, host)
        # 5) M4-2b3/M4-2d：宿主 subProcess 被边界中断 -> 其容器内 timer 事件子流程
        #    订阅及 message/signal 订阅一并失效。普通活动宿主没有容器订阅；
        #    root 兼任 sub 宿主时只撤该 sub 容器的订阅，root 上流程级订阅保留
        if isinstance(host, SubProcess):
            self._drop_scope_event_jobs(pi, token, token.activity_id)
            self._drop_scope_event_subscriptions(pi, token, token.activity_id)

    def _kill_subprocess_scope(self, pi: ProcessInstance, scope: Execution) -> None:
        """中断式 scope 取消：结束 subProcess 内部全部活跃子树（本体除外）。

        scope 本体（subProcess SCOPE）保持 ACTIVE 由调用方结算 actinst 后沿边界
        出边走。逐棵子树复用 _kill_execution_tree（actinst/task/job/join 全清理）。
        """
        for c in list(scope.children):
            self._kill_execution_tree(pi, c)

    def _kill_execution_tree(self, pi: ProcessInstance, e: Execution) -> None:
        """杀灭以 e 为根的整棵执行子树（含 e 本体），全量清理。

        自底向上 kill 每个内部 execution：结算 open actinst、归档待办任务、
        删除所属实例级作业、从 join_arrivals 摘除登记、detach 摘树。e 本体
        同样结算/ENDED/detach。供中断取消（MI 条件终止实例 / 事件中断宿主）。
        """
        now = _now()

        def kill(node: Execution) -> None:
            for c in list(node.children):
                kill(c)
            # 结算未结算的活动实例
            if node.open_activity is not None and node.open_activity.end_time is None:
                node.open_activity.end_time = now
            node.open_activity = None
            # 该 execution 的待办任务归档（中断取消 = HI_TASKINST 留痕）
            for t in [
                t
                for t in self._tasks.values()
                if t.process_instance_id == pi.id and t.execution_id == node.id
            ]:
                self._tasks.pop(t.id)
                t.end_time = now
                pi.completed_tasks.append(t)
            # 该 execution 的实例级作业全部作废
            for j in [
                j
                for j in self._jobs.values()
                if j.process_instance_id == pi.id and j.execution_id == node.id
            ]:
                self._jobs.pop(j.id)
            # M4-2d：该 execution 承载的消息/信号订阅一并作废
            # （catch 停等 token / esc 宿主 scope / 边界宿主）
            for sid in [
                sid
                for sid, s in self._event_subs.items()
                if s.process_instance_id == pi.id and s.execution_id == node.id
            ]:
                self._event_subs.pop(sid, None)
            # 从并行 join 等待登记摘除（内部登记随子树作废）
            for gw, ids in list(pi.join_arrivals.items()):
                if node.id in ids:
                    rest = [i for i in ids if i != node.id]
                    if rest:
                        pi.join_arrivals[gw] = rest
                    else:
                        pi.join_arrivals.pop(gw, None)
            node.state = ExecutionState.ENDED
            self._detach_from_parent(pi, node)

        kill(e)

    def _fire_timer_boundary(self, job: Job) -> None:
        """timer 边界到期：中断式取消宿主 / 非中断式 spawn 并发线（M4-2b4）。

        token 失效防御与 timer-catch 同：宿主已离开/活动已结算 = 过期作业直接
        丢弃（宿主正常离开时边界 job 本应被撤销，此处兜底并发轮询竞态）。
        """
        pi = self._instances.get(job.process_instance_id)
        token = pi.executions.get(job.execution_id) if pi is not None else None
        if (
            pi is None
            or pi.is_completed
            or token is None
            or token.state != ExecutionState.ACTIVE
        ):
            self._jobs.pop(job.id, None)  # 实例/执行已失效 -> 过期作业丢弃
            return
        proc = self._container_of(pi, token)
        boundary = proc.flow_nodes.get(job.node_id)
        host = (
            proc.flow_nodes.get(boundary.attached_to)
            if isinstance(boundary, BoundaryEvent) and boundary.attached_to
            else None
        )
        if host is None:
            self._jobs.pop(job.id, None)  # 防御：异常数据（正常解析后不会发生）
            return
        # 关键校验：宿主活动仍在等待（actinst 未结算）才可触发
        if (
            token.activity_id != host.id
            or token.open_activity is None
            or token.open_activity.end_time is not None
        ):
            self._jobs.pop(job.id, None)  # 宿主已离开 -> 过期作业丢弃
            return
        if boundary.cancel_activity:
            # 中断式：取消宿主活动，token 改走边界事件出边
            self._cancel_host_activity(pi, token, host)
            # 边界事件作为中断路径载体：留 actinst 痕迹后沿其出边推进
            self._open_activity(pi, token, boundary)
            self._close_activity(pi, token, boundary)
            arrivals: List[_Arrival] = []
            self._leave(pi, token, boundary, arrivals)
            self._pump(pi, arrivals)
        else:
            # 非中断式（M4-2b4）：宿主不取消，spawn 并发线从边界事件出边走
            self._spawn_non_interrupting_boundary(pi, token, boundary)

    def _spawn_non_interrupting_boundary(
        self, pi: ProcessInstance, token: Execution, boundary: BoundaryEvent
    ) -> None:
        """非中断式边界触发：宿主保留，并发线（与宿主平级）走边界事件出边。

        并发线是独立执行，不能挂在宿主 token 之下（否则宿主 complete/收束时被
        携带或误判）。挂载点 = 宿主直接父 scope；root 直通宿主无父 -> 挂 root
        （root 兼任实例 scope 与并发线父载体，主线到 end 后转 SCOPE 停驻等收束，
        由 _collapse_scopes 收尾——见 _end_token / M4-2b 收尾段）。

        触发即消费本 timer job（单发，无 repeat -> _reschedule_or_remove 删除），
        宿主其余边界作业保留——宿主仍在等待，其它 timer 边界继续有效。
        """
        parent = pi.executions.get(token.parent_id) if token.parent_id else None
        anchor = parent if parent is not None else token
        line = Execution(
            id=self._idgen.next_id(),
            process_instance_id=pi.id,
            parent_id=anchor.id,
        )
        pi.executions[line.id] = line
        anchor.children.append(line)
        # 边界事件在并发线上留 actinst 痕迹后沿其出边推进（无出边即收束）
        self._open_activity(pi, line, boundary)
        self._close_activity(pi, line, boundary)
        arrivals: List[_Arrival] = []
        self._leave(pi, line, boundary, arrivals)
        self._pump(pi, arrivals)

    # ------------------------------------------------------------------
    # M4-2c：多实例（multiInstanceLoopCharacteristics）
    # ------------------------------------------------------------------
    def _enter_user_task_wait(
        self, pi: ProcessInstance, token: Execution, node: UserTask
    ) -> List[_Arrival]:
        """userTask 宿主进入等待：open actinst + 创建任务 + 注册边界 timer。

        MI 实例启动（_start_mi_instance）复用本方法承载宿主行为。
        """
        self._open_activity(pi, token, node)
        self._create_task(pi, token, node)
        # 宿主进入等待：注册边界 timer（asyncBefore 的 userTask 拆分时已注册，
        # 行为续跑到达此处不重复注册——幂等由 _register_boundary_jobs 保证）
        if not node.async_before:
            self._register_boundary_jobs(pi, token, node)
        return []  # 停等 complete

    def _enter_multi_instance(
        self, pi: ProcessInstance, token: Execution, node: FlowNode
    ) -> List[_Arrival]:
        """token 到达多实例宿主活动：初始化 MI 容器并启动实例（M4-2c）。

        纯 MI 范围（文档化差异）：宿主不允许再组合 asyncBefore/asyncAfter/
        边界事件（组合语义后置里程碑）。实例集求值为空 -> 零实例，立即沿宿主
        出边离开（对齐 Camunda 空集合行为）。
        - sequential：token 自身作容器与实例载体，_start_mi_instance 顺序启动；
        - parallel：token 转 SCOPE 作容器，spawn 每条实例 child execution。
        """
        mi_def = node.multi_instance
        if node.async_before or node.async_after or node.boundary_events:
            raise InvalidRequestException(
                f"多实例宿主 {node.id!r} 暂不支持 asyncBefore/asyncAfter/边界事件"
                "组合（M4-2c 纯多实例语义，组合后续里程碑落地，文档化差异）"
            )
        items, total = self._resolve_mi_set(pi, mi_def)
        if total == 0:
            # 空集合：零次实例，直接沿宿主出边离开
            arrivals: List[_Arrival] = []
            self._leave(pi, token, node, arrivals)
            self._pump(pi, arrivals)
            return []
        container = {
            "total": total,
            "active": 0,
            "completed": 0,
            "next_index": 0,
            "items": items,  # None = loopCardinality 来源（无元素变量）
            "element_variable": mi_def.element_variable,
            "completion_condition": mi_def.completion_condition_expr,
            "sequential": mi_def.sequential,
        }
        token.mi = container
        if mi_def.sequential:
            # 顺序：token 自身执行第 0 个实例（userTask 建任务停等 / subProcess
            # 进内部流转 / serviceTask 同步跑完全部）
            container["active"] = 1
            self._pump(pi, self._start_mi_instance(pi, token, node))
            return []
        # 并行：token 转 SCOPE 容器，spawn N 条实例 child
        token.role = "SCOPE"
        container["active"] = total
        arrivals: List[_Arrival] = []
        for _ in range(total):
            child = Execution(
                id=self._idgen.next_id(),
                process_instance_id=pi.id,
                parent_id=token.id,
            )
            pi.executions[child.id] = child
            token.children.append(child)
            arrivals.extend(self._start_mi_instance(pi, child, node))
            if token.mi is None:
                # 同步宿主（serviceTask）实例在 spawn 期间即时完成并收束容器
                # （全完成或 completionCondition 满足）-> 剩余实例不再启动
                break
        self._pump(pi, arrivals)
        return []

    def _resolve_mi_set(
        self, pi: ProcessInstance, mi_def: MultiInstance
    ) -> Tuple[Optional[List[Any]], int]:
        """实例集求值：collection 表达式 -> 元素列表；loopCardinality -> 数量。

        返回 (items, total)：items=None 表示 cardinality 来源（无元素变量）。
        求值结果非法（非集合 / 非正整数）抛 ProcessInstanceException。
        """
        if mi_def.collection_expr is not None:
            val = evaluate_expression(mi_def.collection_expr, pi.variables)
            if not isinstance(val, (list, tuple, set, frozenset)):
                raise ProcessInstanceException(
                    f"多实例 collection {mi_def.collection_expr!r} 求值须得集合"
                    f"（list/tuple/set），实际: {type(val).__name__}"
                )
            items = list(val)
            return items, len(items)
        if mi_def.loop_cardinality_expr is not None:
            n = evaluate_expression(mi_def.loop_cardinality_expr, pi.variables)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ProcessInstanceException(
                    f"多实例 loopCardinality {mi_def.loop_cardinality_expr!r} "
                    f"求值须得非负整数，实际: {n!r}"
                )
            return None, int(n)
        return None, 0  # 解析期保证两者至少其一；双 None 防御 = 空

    def _start_mi_instance(
        self, pi: ProcessInstance, execution: Execution, node: FlowNode
    ) -> List[_Arrival]:
        """启动下一个实例的宿主行为（容器 next_index 自增），返回后续到达事件。

        行为前注入 loopCounter / elementVariable 到实例变量表（行为期可读）。
        元素变量与 loopCounter 生命周期 = MI 活动执行期（M4-2c 文档化差异：
        不落 ACT_HI_VARINST，容器收尾统一清理）。并行 child 在此登记实例序号
        execution.mi={"index": i}，收束回报计数用。

        M4-2c3 宿主分派：
        - userTask：进入等待（建任务停等，complete_task 驱动实例完成）；
        - serviceTask：同步 delegate 无等待窗口 -> 行为完成即结算实例（顺序容器
          就地循环跑完剩余实例；并行 child 立即完成回报）；
        - subProcess：进入内部流转（等待窗口 = 整段内部执行，collapse 收束链
          驱动实例完成）。
        """
        arrivals: List[_Arrival] = []
        container = (
            execution.mi
            if execution.is_mi_container
            else pi.executions.get(execution.parent_id).mi
        )
        # 实例执行同样携带宿主活动位置（对齐 _handle_arrival 的 activity_id
        # 约定：停等/行为期 activity_id 指向当前活动节点）。顺序容器 token 已由
        # _handle_arrival 设置（幂等），并行 spawn 的 child 在此补齐。
        if not execution.is_mi_container:
            execution.activity_id = node.id
        index = container["next_index"]
        container["next_index"] += 1
        if not execution.is_mi_container:
            execution.mi = {"index": index}  # parallel 实例标识
        self._inject_mi_vars(pi, container, index)
        if isinstance(node, UserTask):
            self._enter_user_task_wait(pi, execution, node)
        elif isinstance(node, ServiceTask):
            # 同步宿主：无等待窗口，行为执行完即结算实例
            if execution.is_mi_container:
                # 顺序容器：token 兼实例载体，就地循环跑完剩余实例
                arrivals = self._run_sequential_service_mi(pi, execution, node)
            else:
                self._run_sync_mi_host(pi, execution, node)
                arrivals = self._complete_mi_instance(
                    pi, execution, node, pi.executions[execution.parent_id]
                )
        elif isinstance(node, SubProcess):
            # subProcess 宿主：进入内部流转（本实例的等待窗口），内部走完由
            # collapse 收束链驱动实例完成
            arrivals = self._enter_subprocess(pi, execution, node)
        else:
            raise ProcessInstanceException(
                f"多实例宿主 {node.id!r} 类型 {type(node).__name__} 不支持"
                "（M4-2c 纯 MI 范围：userTask / serviceTask / subProcess）"
            )
        return arrivals

    def _run_sync_mi_host(
        self, pi: ProcessInstance, execution: Execution, node: ServiceTask
    ) -> None:
        """同步 MI 实例行为：open actinst -> delegate -> close（无等待窗口）。"""
        self._open_activity(pi, execution, node)
        self._run_delegate(pi, execution, node)
        self._close_activity(pi, execution, node)

    def _run_sequential_service_mi(
        self, pi: ProcessInstance, scope: Execution, node: ServiceTask
    ) -> List[_Arrival]:
        """顺序 serviceTask 宿主：当前实例已就绪，同步就地循环跑完剩余实例。

        同步无等待 -> 一条调用链内依次执行各实例：跑完 delegate 即结算计数/条件；
        全部完成或 completionCondition 满足 -> 容器收束沿宿主出边离开（返回事件）。
        """
        mi = scope.mi
        while True:
            self._run_sync_mi_host(pi, scope, node)
            mi["completed"] += 1
            mi["active"] = 0
            self._cleanup_mi_vars(pi, mi)
            if self._mi_done(pi, mi):
                return self._finish_mi_container(pi, scope, node)
            index = mi["next_index"]
            mi["next_index"] += 1
            mi["active"] = 1
            self._inject_mi_vars(pi, mi, index)

    @staticmethod
    def _inject_mi_vars(
        pi: ProcessInstance, container: Dict[str, Any], index: int
    ) -> None:
        """把 loopCounter / elementVariable 注入实例变量表（行为期临时承载）。"""
        pi.variables["loopCounter"] = index
        ev = container["element_variable"]
        if ev:
            items = container["items"] or []
            pi.variables[ev] = items[index] if index < len(items) else None

    @staticmethod
    def _cleanup_mi_vars(pi: ProcessInstance, container: Dict[str, Any]) -> None:
        pi.variables.pop("loopCounter", None)
        ev = container["element_variable"]
        if ev:
            pi.variables.pop(ev, None)

    @staticmethod
    def _mi_condition_vars(
        pi: ProcessInstance, mi: Dict[str, Any]
    ) -> Dict[str, Any]:
        """completionCondition 求值环境：实例变量 + MI 内置计数器。"""
        env = dict(pi.variables)
        env["nrOfInstances"] = mi["total"]
        env["nrOfActiveInstances"] = mi["active"]
        env["nrOfCompletedInstances"] = mi["completed"]
        return env

    def _mi_done(self, pi: ProcessInstance, mi: Dict[str, Any]) -> bool:
        """MI 活动是否应结束：全部实例完成，或 completionCondition 满足。"""
        if mi["completed"] >= mi["total"]:
            return True
        cond = mi["completion_condition"]
        if cond:
            return bool(evaluate_condition(cond, self._mi_condition_vars(pi, mi)))
        return False

    def _mi_scope_of(
        self, pi: ProcessInstance, token: Execution
    ) -> Optional[Execution]:
        """token 完成宿主行为后，其所属 MI 容器（自身顺序容器 / 父并行容器）。"""
        if token.is_mi_container and token.mi["sequential"]:
            return token
        parent = pi.executions.get(token.parent_id) if token.parent_id else None
        if (
            parent is not None
            and parent.is_mi_container
            and not parent.mi["sequential"]
        ):
            return parent
        return None

    def _complete_mi_instance(
        self, pi: ProcessInstance, token: Execution, node: FlowNode, scope: Execution
    ) -> List[_Arrival]:
        """MI 实例完成：计数/条件/收束/续跑，返回后续到达事件（调用方 pump）。

        驱动来源（M4-2c2/2c3）：userTask 宿主由 complete_task；serviceTask 宿主
        delegate 同步完成（_start_mi_instance）；subProcess 宿主内部流转收束
        （_collapse_scopes 复活链）。续跑/收束产生的推进事件在此回传，由调用点
        （complete_task / collapse / spawn 循环）统一 pump，避免深层嵌套 pump。
        """
        mi = scope.mi
        mi["completed"] += 1
        if mi["sequential"]:
            # 顺序：token 即容器。当前实例已完成 -> 决定续跑或收束离开
            mi["active"] = 0
            self._cleanup_mi_vars(pi, mi)
            if self._mi_done(pi, mi):
                return self._finish_mi_container(pi, scope, node)
            mi["active"] = 1
            return self._start_mi_instance(pi, scope, node)  # 下一个实例
        # 并行：child 完成收束 -> 检查条件（满足则终止剩余活跃实例）
        mi["active"] = max(0, mi["active"] - 1)
        token.state = ExecutionState.ENDED
        self._detach_from_parent(pi, token)
        if mi["completed"] < mi["total"] and self._mi_done(pi, mi):
            self._kill_mi_active_children(pi, scope)
        if self._mi_done(pi, mi):
            return self._finish_mi_container(pi, scope, node)
        return []

    def _kill_mi_active_children(self, pi: ProcessInstance, scope: Execution) -> None:
        """completionCondition 提前满足：终止 scope 下仍活跃的并行实例。

        对齐 Camunda：多实例活动在条件满足时结束，剩余未完成实例被取消——
        待办任务归档（HI_TASKINST 带 end_time）、结算 actinst、清理实例级作业。
        实例载体可能是叶子（userTask/serviceTask host child）也可能是子树
        （subProcess host 实例 scope），统一按整树杀灭处理。
        """
        mi = scope.mi
        for child in list(scope.children):
            if child.state != ExecutionState.ACTIVE:
                continue
            # 被终止实例 = 取消而非完成：仅回落 active，不动 completed
            mi["active"] = max(0, mi["active"] - 1)
            self._kill_execution_tree(pi, child)
        scope.children = [
            c for c in scope.children if c.state == ExecutionState.ACTIVE
        ]

    def _finish_mi_container(
        self, pi: ProcessInstance, scope: Execution, node: FlowNode
    ) -> List[_Arrival]:
        """MI 活动收束：清容器状态与注入变量，恢复 TOKEN 沿宿主出边离开。

        离开推进事件返回调用方统一 pump（调用点可能在 pump 内也可能在
        complete_task 等外部入口——事件回传而非嵌套 pump，避免调用链加深）。
        """
        self._cleanup_mi_vars(pi, scope.mi)
        scope.mi = None
        scope.role = "TOKEN"
        arrivals: List[_Arrival] = []
        self._leave(pi, scope, node, arrivals)
        return arrivals

    # ------------------------------------------------------------------
    # 并行网关 fork / join
    # ------------------------------------------------------------------
    def _handle_parallel_gateway(
        self, pi: ProcessInstance, token: Execution, gw: ParallelGateway
    ) -> List[_Arrival]:
        proc = self._container_of(pi, token)
        arrivals: List[_Arrival] = []
        incoming = self._incoming(proc, gw)

        # join 分支：到达数 == 入边数 才汇聚，否则停等
        if len(incoming) > 1:
            pi.register_join_arrival(gw.id, token.id)
            if len(pi.join_arrived(gw.id)) < len(incoming):
                return arrivals  # 等待其余分支
            # 汇聚完成：记录网关活动实例，SCOPE 恢复继续
            self._open_activity(pi, token, gw)
            self._close_activity(pi, token, gw)
            self._end_waiting_tokens(pi, gw)
            scope = self._find_scope(pi, token)
            actor = scope if scope is not None else token
            # M4-2a：汇聚后 SCOPE 恢复为主线角色（否则停在普通等待节点时可能被
            # 收束扫描误杀）；并行 fork 多出边会再次置回 SCOPE
            actor.role = "TOKEN"
            self._leave(pi, actor, gw, arrivals)
            return arrivals

        # fork 分支：分裂出边
        self._open_activity(pi, token, gw)
        self._close_activity(pi, token, gw)
        self._leave(pi, token, gw, arrivals)
        return arrivals

    def _leave(
        self,
        pi: ProcessInstance,
        token: Execution,
        node: FlowNode,
        arrival_list: List[_Arrival],
    ) -> None:
        """离开节点：1 条出边直通；多条出边 fork（token 转 SCOPE，spawn 子）。"""
        proc = self._container_of(pi, token)
        flows = self._outgoing(proc, node)
        if not flows:
            # 无出边（流程末端）：结束 token；可能连带触发子流程 scope 收束复活
            arrival_list.extend(self._end_token(pi, token))
            return
        if len(flows) == 1:
            self._take(pi, token, flows[0], arrival_list)
            return
        # fork
        token.role = "SCOPE"
        for flow in flows:
            child = Execution(
                id=self._idgen.next_id(),
                process_instance_id=pi.id,
                parent_id=token.id,
            )
            pi.executions[child.id] = child
            token.children.append(child)
            target = proc.flow_nodes[flow.target_ref]
            arrival_list.append((child, target))

    def _end_waiting_tokens(self, pi: ProcessInstance, gw: ParallelGateway) -> None:
        """join 汇聚后：结束停等 token 并从父树摘除。"""
        for wid in pi.join_arrived(gw.id):
            waiting = pi.executions.get(wid)
            if waiting is None:
                continue
            waiting.state = ExecutionState.ENDED
            self._detach_from_parent(pi, waiting)
        pi.clear_join_arrivals(gw.id)

    def _find_scope(self, pi: ProcessInstance, token: Execution) -> Optional[Execution]:
        """向上找最近的 SCOPE 父（join 汇聚后由其承担恢复推进）。"""
        cur = token
        while cur.parent_id is not None:
            parent = pi.executions.get(cur.parent_id)
            if parent is None:
                break
            if parent.role == "SCOPE":
                return parent
            cur = parent
        return None

    def _take(
        self,
        pi: ProcessInstance,
        token: Execution,
        flow: SequenceFlow,
        arrival_list: List[_Arrival],
    ) -> None:
        proc = self._container_of(pi, token)
        target = proc.flow_nodes[flow.target_ref]
        arrival_list.append((token, target))

    # ------------------------------------------------------------------
    # token 收束 / 活动历史
    # ------------------------------------------------------------------
    def _end_token(self, pi: ProcessInstance, token: Execution) -> List[_Arrival]:
        """token 到达 endEvent / 无出边：结束自己并尝试收束（返回复活推进事件）。

        根结束 = 主线走完；若 root 还有活跃子执行（非中断边界并发线 / 非中断
        事件子流程，M4-2b4），root 转 SCOPE 停驻等子树全收束后才完成实例
        （_collapse_scopes 收尾段兜底）。子 token 结束 -> 自底向上收束 SCOPE
        （并行分支各自走完 / 子流程内部走完），期间可能复活 subProcess SCOPE
        沿其出边推进。
        """
        if token.is_root or pi.root_execution is token:
            alive = [
                c for c in token.children if c.state == ExecutionState.ACTIVE
            ]
            if alive:
                # 主线已到 end 但并发子树未收束：root 脱离活动节点停驻（activity
                # 清空 -> collapse 不会把它当 sub/网关收，子树收束后收尾段完成）
                token.role = "SCOPE"
                token.activity_id = None
                return []  # 等子树收束；_collapse_scopes 收尾段完成实例
            token.state = ExecutionState.ENDED
            self._detach_from_parent(pi, token)
            self._complete_instance(pi)
            return []
        token.state = ExecutionState.ENDED
        self._detach_from_parent(pi, token)
        return self._collapse_scopes(pi)

    def _detach_from_parent(self, pi: ProcessInstance, token: Execution) -> None:
        parent = pi.executions.get(token.parent_id) if token.parent_id else None
        if parent is not None and token in parent.children:
            parent.children.remove(token)

    def _collapse_scopes(self, pi: ProcessInstance) -> List[_Arrival]:
        """自底向上收束已无活跃子的 SCOPE（M4-2a 泛化：任意层，返回复活推进事件）。

        叶子判定：SCOPE 且 children 已空（直接子全 ENDED 被 detach）：
        - 停在 ParallelGateway（fork 停驻，分支直通 end、无 join 汇聚）-> 结束
          自身（root 结束 = 实例完成），逐层向上继续收；
        - 停在 SubProcess（内部全部走完）-> 复活：结算 subProcess actinst、
          恢复 TOKEN 沿 sub 出边推进，产生新到达事件交由调用方 pump；
        - 停在普通节点（如 join 汇聚后主线停在 userTask）-> 主线身份，不收
          （join 汇聚恢复时已把 role 复位 TOKEN，这里双重防御）。
        """
        root = pi.root_execution
        if root is None or root.state == ExecutionState.ENDED:
            return []
        arrivals: List[_Arrival] = []
        changed = True
        while changed:
            changed = False
            for e in list(pi.executions.values()):
                if e.state != ExecutionState.ACTIVE or e.role != "SCOPE":
                    continue
                if e.children:
                    continue  # 仍有活跃子树（含子流程内部执行中）
                node = (
                    self._container_of(pi, e).flow_nodes.get(e.activity_id)
                    if e.activity_id
                    else None
                )
                if isinstance(node, SubProcess):
                    # 子流程内部全部收束：结算 actinst / 边界 timer / 容器订阅
                    self._close_activity(pi, e, node)
                    self._drop_boundary_jobs(pi, node)  # 正常离开：sub 边界 timer 作废
                    # M4-2b3/M4-2d：该 sub 容器 esc 订阅一并失效（timer job +
                    # message/signal 订阅；root 兼任时流程级订阅保留）
                    self._drop_scope_event_jobs(pi, e, node.id)
                    self._drop_scope_event_subscriptions(pi, e, node.id)
                    if node.multi_instance is not None:
                        # M4-2c3：subProcess 宿主多实例——e 是顺序容器自身或某并行
                        # 实例载体，本实例内部流转已走完 -> 走实例完成路径（计数/
                        # 续跑下一实例/条件/收束容器），推进事件并入 arrivals
                        mi_scope = self._mi_scope_of(pi, e)
                        if mi_scope is not None:
                            arrivals.extend(
                                self._complete_mi_instance(pi, e, node, mi_scope)
                            )
                            changed = True
                            continue
                    e.role = "TOKEN"  # 恢复主线身份（后续 fork 会再置回 SCOPE）
                    self._leave(pi, e, node, arrivals)
                    changed = True
                elif isinstance(node, ParallelGateway):
                    # fork 停驻且分支全结束（无 join 汇聚路径）-> 结束自身向上收
                    e.state = ExecutionState.ENDED
                    self._detach_from_parent(pi, e)
                    if e.is_root or pi.root_execution is e:
                        self._complete_instance(pi)
                    changed = True
        # M4-2b：root 被流程级事件子流程接管后（中断清空 activity/子树），事件
        # 子流程 scope 收束完毕 -> 宿主 scope（流程实例）结束 = 实例完成。
        # root 先置 ENDED：收尾段路径下主线早已到 end（root 转停驻 SCOPE），
        # 收束完成即实例结束，避免 ACTIVE root 残行被 _build_snap 写回 RU。
        root = pi.root_execution
        if (
            root is not None
            and root.state == ExecutionState.ACTIVE
            and not root.children
            and root.activity_id is None
            and root.open_activity is None
        ):
            root.state = ExecutionState.ENDED
            self._complete_instance(pi)
        return arrivals

    def _complete_instance(self, pi: ProcessInstance) -> None:
        pi.state = ProcessInstanceState.COMPLETED
        pi.end_time = _now()
        # M4-2b3：实例完成 -> 该实例全部实例级作业作废（订阅/边界/catch 无残留）
        for jid in [
            j.id
            for j in self._jobs.values()
            if j.process_instance_id == pi.id
        ]:
            self._jobs.pop(jid, None)

    # ------------------------------------------------------------------
    # 活动实例历史
    # ------------------------------------------------------------------
    def _open_activity(self, pi: ProcessInstance, token: Execution, node: FlowNode) -> None:
        # asyncBefore 拆分时已 open（token.open_activity 未结算）-> 行为续跑复用，
        # 避免 async job 执行时重复记一条 actinst
        if (
            token.open_activity is not None
            and token.open_activity.activity_id == node.id
            and token.open_activity.end_time is None
        ):
            return
        ai = ActivityInstance(
            id=self._idgen.next_id(),
            process_instance_id=pi.id,
            activity_id=node.id,
            activity_name=node.name,
            execution_id=token.id,
            start_time=_now(),
        )
        token.open_activity = ai
        pi.activity_history.append(ai)

    def _close_activity(self, pi: ProcessInstance, token: Execution, node: FlowNode) -> None:
        ai = token.open_activity
        if ai is not None and ai.activity_id == node.id and ai.end_time is None:
            ai.end_time = _now()
        token.open_activity = None

    def _create_task(self, pi: ProcessInstance, token: Execution, node: UserTask) -> Task:
        task = Task(
            id=self._idgen.next_id(),
            name=node.name,
            process_instance_id=pi.id,
            execution_id=token.id,
            task_definition_key=node.id,
            assignee=node.assignee,
            candidate_users=list(node.candidate_users),
            candidate_groups=list(node.candidate_groups),
            create_time=_now(),
        )
        self._tasks[task.id] = task
        return task

    def _run_delegate(self, pi: ProcessInstance, token: Execution, node: ServiceTask) -> None:
        ref = node.implementation_ref
        if ref is None:
            return  # 无实现的 serviceTask 视为 pass-through
        fn = self._delegates.get(ref)
        if fn is None:
            raise ProcessInstanceException(
                f"serviceTask {node.id!r} 引用未注册的 delegate: {ref!r}"
            )
        result = fn(pi.variables)
        if isinstance(result, dict):
            pi.variables.update(result)

    # ------------------------------------------------------------------
    # 静态出/入边辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _outgoing(proc: Process, node: FlowNode) -> List[SequenceFlow]:
        return [proc.sequence_flows[fid] for fid in node.outgoing]

    @staticmethod
    def _incoming(proc: Process, node: FlowNode) -> List[SequenceFlow]:
        return [proc.sequence_flows[fid] for fid in node.incoming]

    # ------------------------------------------------------------------
    # M2/M3：持久化快照 / 崩溃恢复
    # ------------------------------------------------------------------
    def _build_snap(self, pi: ProcessInstance) -> "ProcInstSnap":
        """把实例内存态转为落库快照（只含 ACTIVE execution / 活跃任务 / 实例级 job）。"""
        from camunda.persistence.store import (
            ActivitySnap,
            ExecutionSnap,
            JobSnap,
            ProcInstSnap,
            TaskSnap,
        )

        # 按树序输出 execution（ACTIVE 才落 RU）
        active: List[Execution] = []
        stack = [pi.root_execution] if pi.root_execution else []
        while stack:
            e = stack.pop()
            if e is None:
                continue
            if e.state == ExecutionState.ACTIVE:
                active.append(e)
            stack.extend(reversed(e.children))  # 保持父->子顺序稳定

        active_tasks = [t for t in self._tasks.values() if t.process_instance_id == pi.id]
        pi_jobs = [j for j in self._jobs.values() if j.process_instance_id == pi.id]

        return ProcInstSnap(
            id=pi.id,
            process_definition_key=pi.process_definition_key,
            business_key=pi.business_key,
            state=pi.state.value,
            start_time=pi.start_time or "",
            end_time=pi.end_time,
            variables=dict(pi.variables),
            executions=[
                ExecutionSnap(
                    id=e.id,
                    parent_id=e.parent_id,
                    activity_id=e.activity_id,
                    role=e.role,
                    mi=e.mi,  # M4-2c4：MI 容器/实例状态随 execution 落库
                )
                for e in active
            ],
            tasks=[
                TaskSnap(
                    id=t.id,
                    name=t.name,
                    execution_id=t.execution_id,
                    task_definition_key=t.task_definition_key,
                    assignee=t.assignee,
                    create_time=t.create_time or "",
                    end_time=None,
                )
                for t in active_tasks
            ],
            jobs=[
                JobSnap(
                    id=j.id,
                    job_type=j.job_type,
                    execution_id=j.execution_id,
                    node_id=j.node_id,
                    duedate=j.duedate,
                    created=j.created,
                    retries=j.retries,
                    repeat=j.repeat,
                )
                for j in pi_jobs
            ],
            activity_history=[
                ActivitySnap(
                    id=a.id,
                    activity_id=a.activity_id,
                    activity_name=a.activity_name,
                    execution_id=a.execution_id,
                    start_time=a.start_time,
                    end_time=a.end_time,
                )
                for a in pi.activity_history
            ],
            completed_tasks=[
                TaskSnap(
                    id=t.id,
                    name=t.name,
                    execution_id=t.execution_id,
                    task_definition_key=t.task_definition_key,
                    assignee=t.assignee,
                    create_time=t.create_time or "",
                    end_time=t.end_time,
                )
                for t in pi.completed_tasks
            ],
        )

    @classmethod
    def from_database(cls, url: str) -> "ProcessEngine":
        """从数据库恢复引擎：加载部署定义 + 所有运行中实例。

        相当于 Camunda 重启后 JobExecutor/引擎重新挂载 ACT_RU_* 状态。
        注意：恢复后需要自行 register_delegate 同名实现（bean 注册不落库）。
        """
        from camunda.persistence.store import Store
        from camunda.parser.bpmn_parser import parse_bpmn_xml

        store = Store(url)
        engine = cls(store=store)

        # 1) 恢复部署：每 key 取最新版本重解析
        latest: Dict[str, dict] = {}
        for d in store.load_proc_defs():
            cur = latest.get(d["key"])
            if cur is None or d["version"] > cur["version"]:
                latest[d["key"]] = d
        for d in latest.values():
            xml = d["xml"]
            if not xml:
                continue
            model = parse_bpmn_xml(xml, source_name=d["key"])
            for proc in model.processes:
                engine._definitions[proc.id] = proc
                engine._definition_versions[proc.id] = d["version"]

        # 2) 恢复定义级作业（timer-start：PROC_INST_ID_ IS NULL）
        for j in store.load_timer_start_jobs():
            if j.process_definition_key in engine._definitions:
                engine._jobs[j.id] = j

        # 3) 恢复运行中实例（RU 表，含实例级 job）
        for snap in store.load_active_instances():
            engine._restore_instance(snap)
        return engine

    def _restore_instance(self, snap: "ProcInstSnap") -> None:
        """把库中活跃实例快照重建为内存运行时态。"""
        # 根 = parent_id 为空的 execution（RU 行父在前已保证插入顺序）
        by_id: Dict[str, Execution] = {}
        root: Optional[Execution] = None
        for ex in snap.executions:
            e = Execution(
                id=ex.id,
                process_instance_id=snap.id,
                parent_id=ex.parent_id,
                role=ex.role,
                activity_id=ex.activity_id,
                # M4-2c4：MI 容器/实例状态还原（浅拷贝防快照对象复用共享；
                # items 元素只读，容器计数/序号随实例完成在原位自增）
                mi=dict(ex.mi) if ex.mi else None,
                state=ExecutionState.ACTIVE,
            )
            by_id[e.id] = e
            if ex.parent_id is None:
                root = e
        for e in by_id.values():
            if e.parent_id and e.parent_id in by_id:
                by_id[e.parent_id].children.append(e)

        pi = ProcessInstance(
            id=snap.id,
            process_definition_key=snap.process_definition_key,
            business_key=snap.business_key,
            state=ProcessInstanceState(snap.state),
            variables=dict(snap.variables),
            root_execution=root,
            start_time=snap.start_time,
            end_time=snap.end_time,
            executions=by_id,
        )
        # 活动历史还原（ACT_HI_ACTINST 快照）
        for a in snap.activity_history:
            ai = ActivityInstance(
                id=a.id,
                process_instance_id=snap.id,
                activity_id=a.activity_id,
                activity_name=a.activity_name,
                execution_id=a.execution_id,
                start_time=a.start_time,
                end_time=a.end_time,
            )
            pi.activity_history.append(ai)
            # 未结算的活动（如停在 userTask）挂回对应 execution，便于后续 close
            if a.end_time is None:
                owner = by_id.get(a.execution_id)
                if owner is not None:
                    owner.open_activity = ai
        # join 等待状态还原：停在并行网关的 ACTIVE TOKEN 即 join 等待
        # （M4-2a：按各自所在容器解析节点类型，子流程内部 join 同样可还原）
        for e in by_id.values():
            if (
                e.state == ExecutionState.ACTIVE
                and e.activity_id
                and e.role != "SCOPE"  # SCOPE 停驻 fork 网关不算 join 等待
            ):
                node = self._container_of(pi, e).flow_nodes.get(e.activity_id)
                if isinstance(node, ParallelGateway):
                    pi.register_join_arrival(e.activity_id, e.id)
        # 任务还原
        for t in snap.tasks:
            task = Task(
                id=t.id,
                name=t.name,
                process_instance_id=snap.id,
                execution_id=t.execution_id,
                task_definition_key=t.task_definition_key,
                assignee=t.assignee,
                create_time=t.create_time,
            )
            self._tasks[task.id] = task
        # 已归档任务还原（HI_TASKINST 带 end_time 跨重启保留；HI 全量重写语义下
        # 缺了它会让重启后的下一次 save 抹掉历史——见 M4-2b5 修复记录）
        for t in snap.completed_tasks:
            task = Task(
                id=t.id,
                name=t.name,
                process_instance_id=snap.id,
                execution_id=t.execution_id,
                task_definition_key=t.task_definition_key,
                assignee=t.assignee,
                create_time=t.create_time,
                end_time=t.end_time,
            )
            pi.completed_tasks.append(task)
        # 实例级作业还原（timer-catch / async-continuation 停等）
        for j in snap.jobs:
            self._jobs[j.id] = Job(
                id=j.id,
                job_type=j.job_type,
                duedate=j.duedate,
                created=j.created,
                process_instance_id=snap.id,
                execution_id=j.execution_id,
                node_id=j.node_id,
                retries=j.retries,
                repeat=j.repeat,
            )
        self._rebuild_event_subscriptions(pi)
        self._instances[snap.id] = pi

    def _rebuild_event_subscriptions(self, pi: ProcessInstance) -> None:
        """崩溃恢复：从执行树重推导消息/信号订阅（M4-2d4，纯内存派生态）。

        订阅不落库（对齐 join 等待「恢复时从树推导」先例）：按「停驻状态 =
        等待状态」重放注册，规则与运行期注册点一一对应：
        - root 未完成 -> 流程级容器 esc message/signal start 订阅（root 直通/
          兼任 sub 时与运行期一致双注册）；
        - 停驻 SubProcess 的 SCOPE（actinst open）-> 该 sub 容器内 esc 订阅；
          embedded sub 宿主等待窗口 = 整段内部执行 -> 边界订阅同样重放
          （事件子流程 scope 运行期不注册边界，保持一致不重放）；
        - 停在 message/signal catch 的 token -> catch 订阅；
        - 停在挂边界事件的宿主（userTask / asyncBefore 节点，actinst open）->
          边界订阅。timer 边界/esc job 已随 jobs 快照还原，幂等检查防止重放
          时重复注册 timer（只补 message/signal 订阅）。
        """
        if pi.is_completed or pi.root_execution is None:
            return
        root_proc = self._definitions[pi.process_definition_key]
        self._register_event_subprocess_subscriptions(
            pi, pi.root_execution, root_proc, None
        )
        for e in list(pi.executions.values()):
            if e.state != ExecutionState.ACTIVE or not e.activity_id:
                continue
            waiting = e.open_activity is not None and e.open_activity.end_time is None
            if not waiting:
                continue
            node = self._container_of(pi, e).flow_nodes.get(e.activity_id)
            if node is None:
                continue
            if e.role == "SCOPE" and isinstance(node, SubProcess):
                # 容器激活（嵌入/事件子流程 scope）：重放容器内 esc 订阅；
                # 嵌入子流程宿主等待窗口 = 整段内部执行 -> 边界订阅同样重放
                self._register_event_subprocess_subscriptions(
                    pi, e, node.process, node.id
                )
                if not node.triggered_by_event:
                    self._register_boundary_jobs(pi, e, node)
                continue
            if e.role == "SCOPE":
                continue  # fork 网关等其它 SCOPE 停驻无订阅
            if isinstance(node, IntermediateCatchEvent):
                if node.message_name is not None or node.signal_name is not None:
                    sub = EventSubscription(
                        id=self._idgen.next_id(),
                        kind="message" if node.message_name is not None else "signal",
                        event_name=node.message_name or node.signal_name,
                        process_instance_id=pi.id,
                        execution_id=e.id,
                        activity_id=None,
                        node_id=node.id,
                        node_kind="catch",
                        is_interrupting=True,
                        created=_now(),
                    )
                    self._event_subs[sub.id] = sub
                continue
            if isinstance(node, UserTask) or node.async_before:
                # 宿主等待活动：重放边界订阅（timer job 已还原，幂等跳过）
                self._register_boundary_jobs(pi, e, node)

    # ------------------------------------------------------------------
    # M3：定义级 timer-start 作业（不挂实例，随部署生命周期）
    # ------------------------------------------------------------------
    def _make_timer_start_job(self, proc_key: str, start: StartEvent) -> Job:
        """按 timer kind 构造 timer-start 作业，duedate = 首次触发时刻。

        - date    : 绝对触发时间点（parse_trigger_date 归一化本地时区）
        - duration: 相对部署时刻的一次性延迟
        - cycle   : 周期重复。value 为 ISO R[n]/dur 时 repeat={"kind":"interval"}；
                    否则视为 quartz/cron 表达式（croniter 求值）。无限续排
        """
        timer = start.timer
        now = _now()
        if timer.kind == "date":
            duedate = parse_trigger_date(timer.value)
            repeat = None
        elif timer.kind == "duration":
            duedate = format_iso(
                parse_iso(now) + timedelta(seconds=timer.delay_seconds or 0)
            )
            repeat = None
        else:  # cycle
            repeat = parse_iso_repeat(timer.value)
            if repeat is None:  # 非 ISO 重复 => quartz/cron 表达式
                repeat = {"kind": "cron", "expr": timer.value.strip()}
            duedate = format_iso(next_trigger(repeat, parse_iso(now)))
        return Job(
            id=self._idgen.next_id(),
            job_type="timer-start",
            duedate=duedate,
            created=now,
            process_definition_key=proc_key,
            node_id=start.id,
            repeat=repeat,
        )

    def _drop_definition_jobs(self, proc_key: str) -> None:
        """移除某 process key 的全部定义级作业（重部署新版本前调用）。"""
        stale = [
            j.id
            for j in self._jobs.values()
            if j.is_definition_level and j.process_definition_key == proc_key
        ]
        for jid in stale:
            del self._jobs[jid]

    def _definition_level_jobs(self) -> List[Job]:
        """当前全部定义级作业（timer-start 组）。"""
        return [j for j in self._jobs.values() if j.is_definition_level]

    def _sync_timer_start_jobs(self) -> None:
        """store 模式：定义级作业组全量落库（部署 / 触发续排 / 删除时调用）。"""
        if self._store is not None:
            self._store.save_timer_start_jobs(self._definition_level_jobs())

    # ------------------------------------------------------------------
    # M3：JobExecutor 引擎侧（execute_due_jobs 即 Camunda JobExecutor 轮询）
    # ------------------------------------------------------------------
    def execute_due_jobs(
        self,
        limit: Optional[int] = None,
        *,
        lock_owner: Optional[str] = None,
        lease_seconds: int = 300,
    ) -> int:
        """执行当前到期且非死信的作业，返回执行条数。

        JobExecutor 轮询线程与手动触发都调本方法（内部持引擎锁，与用户命令
        互斥）。续排出的新作业不在本次快照内，留待下一轮询 tick —— 周期作业
        不补触发（错过即错过，interval 按计划链续排不漂移）。

        lock_owner is not None 且 self._store 不为空时（M7）：走 DB CAS lease
        抢锁路径，多 JobExecutor / 多进程场景下保证同一作业只被一个节点执行。
        否则走原内存路径（单进程兼容）。
        """
        if lock_owner is not None and self._store is not None:
            return self._execute_due_jobs_db(
                lock_owner, lease_seconds, limit if limit is not None else 50
            )
        with self._lock:
            due = sorted(
                (
                    j
                    for j in self._jobs.values()
                    if j.is_due(_now()) and not j.is_dead()
                ),
                key=lambda j: j.duedate,
            )
            if limit is not None:
                due = due[:limit]
            executed = 0
            for job in due:
                if self._jobs.get(job.id) is None:
                    continue  # 前序作业执行已连带删除（防御）
                try:
                    self._execute_job(self._jobs[job.id])
                except Exception:  # pragma: no cover - _execute_job 内部已兜底
                    logger.exception("job %s 执行出现未预期异常", job.id)
                executed += 1
            return executed

    def _execute_due_jobs_db(
        self, lock_owner: str, lease_seconds: int, batch_size: int
    ) -> int:
        """DB 抢锁路径（M7）：用 store.acquire_due_jobs 拿到一批 due job，
        对每个 CAS 抢到的作业复用内存 _run_job_body 执行，结果用
        complete_job_cas / reschedule_job_cas 写回（不调 _persist_job_state
        全量重写，避免与并发 JobExecutor 争 LOCK 列）。

        防御要点：
        - 内存里没有该 job（被别的节点推进 / 删了）-> 直接 CAS complete
        - CAS 写回失败（owner 已变更 = lease 过期被抢）-> 跳过（防御），
          但内存里的 _reschedule_or_remove / _degrade_after_failure 已改了
          mem_job，需要再次 save_proc_inst 落库让 DB 与内存一致
        - 失败回滚会重建内存 job（LOCK 列从 DB 读回）-> 用函数参数 owner
          而非 mem_job.lock_owner 做 CAS，保证身份一致
        """
        acquired = self._store.acquire_due_jobs(
            lock_owner, lease_seconds, _now(), batch_size
        )
        if not acquired:
            return 0
        executed = 0
        for snap_job in acquired:
            mem_job = self._jobs.get(snap_job.id)
            if mem_job is None:
                # 内存没有（实例被别节点推进 / 删了），DB 行直接清掉
                self._store.complete_job_cas(snap_job.id, lock_owner)
                continue
            try:
                self._run_job_body(mem_job)
            except Exception:
                logger.warning(
                    "db-locked job %s (%s@%s) 执行失败，剩余重试 %d",
                    mem_job.id,
                    mem_job.job_type,
                    mem_job.node_id,
                    mem_job.retries - 1,
                    exc_info=True,
                )
                # 实例级失败：rollback 到上次同步点（DB LOCK 保留，内存重建后
                # lock_owner 是 None，CAS 必须用函数参数 owner）
                if mem_job.process_instance_id is not None:
                    self._rollback_instance(mem_job.process_instance_id)
                    mem_job = self._jobs.get(mem_job.id) or mem_job
                self._degrade_after_failure(mem_job)
                # CAS 写回（clear_lock=True：失败后让其他 JobExecutor 能看到
                # 新的 retries / duedate；duedate 已推到 retry_delay 之后，
                # 不会立刻被重抢）
                self._store.reschedule_job_cas(
                    mem_job.id,
                    lock_owner,
                    mem_job.duedate,
                    mem_job.retries,
                    clear_lock=True,
                )
                # 实例级还要把实例快照全量落库（save_proc_inst 走原路径，
                # 不带 LOCK 列，与 CAS 写回的 LOCK 清掉语义吻合）
                if (
                    mem_job.process_instance_id is not None
                    and self._instances.get(mem_job.process_instance_id) is not None
                ):
                    pi = self._instances[mem_job.process_instance_id]
                    self._store.save_proc_inst(self._build_snap(pi))
                elif mem_job.is_definition_level:
                    self._sync_timer_start_jobs()
                executed += 1
                continue
            # 成功：一次性 / 续排
            self._reschedule_or_remove(mem_job)
            if mem_job.id not in self._jobs:
                # 一次性作业：从 DB 删除（CAS 防御 owner 不匹配）
                self._store.complete_job_cas(mem_job.id, lock_owner)
            else:
                # 续排（timer-start cycle）：CAS 更新 duedate + 清 LOCK
                self._store.reschedule_job_cas(
                    mem_job.id,
                    lock_owner,
                    mem_job.duedate,
                    mem_job.retries,
                    clear_lock=True,
                )
            # 同步实例级状态（Execution / Task / Variable）到 DB：
            # 一次性作业执行时可能推进 token（创建/删除 execution、新增 task、
            # 改变量），必须 save_proc_inst 全量重写该实例的 RU 行，
            # 否则重启后这些变更丢失。timer-start 的实例未变，仍在 _definitions；
            # _sync_timer_start_jobs 由 reschedule_job_cas 已处理（清 LOCK）。
            if (
                mem_job.process_instance_id is not None
                and self._instances.get(mem_job.process_instance_id) is not None
            ):
                pi = self._instances[mem_job.process_instance_id]
                self._store.save_proc_inst(self._build_snap(pi))
            elif mem_job.is_definition_level:
                # 防御：续例 + 续排路径下也再 sync 一次（reschedule_job_cas
                # 已写 ACT_RU_JOB 行，但 LOCK 列状态以那里为准）
                self._sync_timer_start_jobs()
            executed += 1
        return executed

    def create_job_query(self, process_instance_id: Optional[str] = None) -> List[Job]:
        """查看作业（对齐 createJobQuery，按 duedate 升序；死信 retries==0 可见）。"""
        with self._lock:
            jobs = list(self._jobs.values())
            if process_instance_id is not None:
                jobs = [j for j in jobs if j.process_instance_id == process_instance_id]
            return sorted(jobs, key=lambda j: j.duedate)

    def delete_job(self, job_id: str) -> None:
        """手动删除作业（对齐 ManagementService.deleteJob）。

        注意：删除 timer-catch/async 作业后对应 token 将永久停驻 —— Camunda
        语义相同（删 job 即放弃该次调度），M3 不做额外护栏。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise NotFoundException(f"作业不存在: {job_id!r}")
            del self._jobs[job_id]
            if self._store is not None:
                if job.is_definition_level:
                    self._sync_timer_start_jobs()
                else:
                    pi = self._instances.get(job.process_instance_id)
                    if pi is not None:
                        self._store.save_proc_inst(self._build_snap(pi))

    # ------------------------------------------------------------------
    # M3：单条作业执行
    # ------------------------------------------------------------------
    def _execute_job(self, job: Job) -> None:
        """执行一条作业：成功 -> 删除/续排 + 落库；失败 -> 重试降级 + 落库。

        失败不向外抛（避免单条失败打断整轮轮询）。持久化模式实例级失败会先
        回滚到上次同步点再降级，避免「内存已推进、库未写」的半执行不一致。
        """
        try:
            self._run_job_body(job)
        except Exception:
            logger.warning(
                "job %s (%s@%s) 执行失败，剩余重试 %d",
                job.id,
                job.job_type,
                job.node_id,
                job.retries - 1,
                exc_info=True,
            )
            if job.process_instance_id is not None and self._store is not None:
                # 实例级 + 持久化：整体回滚到上次同步点（RU/HI 未动，内存重建）
                self._rollback_instance(job.process_instance_id)
            job = self._jobs.get(job.id) or job  # rollback 重建了 job 对象
            self._degrade_after_failure(job)
            if self._store is not None:
                self._persist_job_state(job)
        else:
            self._reschedule_or_remove(job)
            if self._store is not None:
                self._persist_job_state(job)

    def _run_job_body(self, job: Job) -> None:
        """纯执行单条作业（不落库）。失败向上抛，调用方决定降级 / 续排策略。

        拆出来供 DB 抢锁路径（M7）复用：CAS 写回而非全量重写，避免与
        其他 JobExecutor 产生 LOCK 列竞态。
        """
        if job.job_type == "timer-start":
            self._fire_timer_start(job)
        elif job.job_type == "timer-catch":
            self._fire_timer_catch(job)
        elif job.job_type == "timer-boundary":
            self._fire_timer_boundary(job)
        elif job.job_type == "timer-event-start":
            self._fire_timer_event_start(job)
        elif job.job_type == "async-continuation":
            self._run_async_continuation(job)
        elif job.job_type == "async-after":
            self._run_async_after(job)
        else:
            raise InvalidRequestException(f"未知作业类型: {job.job_type!r}")

    def _fire_timer_start(self, job: Job) -> None:
        """timer-start 触发：启动一个流程实例（cycle 续排由 _reschedule_or_remove 处理）。"""
        proc = self._definitions[job.process_definition_key]
        start = proc.flow_nodes[job.node_id]
        if not isinstance(start, StartEvent):
            raise ProcessInstanceException(
                f"timer-start job {job.id} 指向非 startEvent 节点: {job.node_id!r}"
            )
        # _start_process 内部已落库实例（store 模式）；定时启动不带用户变量
        self._start_process(proc, None, None, start)

    def _fire_timer_catch(self, job: Job) -> None:
        """timer-catch 到期：结算停等 actinst，token 沿出边继续推进。"""
        pi = self._instances.get(job.process_instance_id)
        token = pi.executions.get(job.execution_id) if pi is not None else None
        if (
            pi is None
            or token is None
            or token.state != ExecutionState.ACTIVE
            or token.activity_id != job.node_id
        ):
            self._jobs.pop(job.id, None)  # token 已失效/推进 -> 过期作业直接丢弃
            return
        proc = self._container_of(pi, token)
        node = proc.flow_nodes[job.node_id]
        self._close_activity(pi, token, node)
        arrivals: List[_Arrival] = []
        self._leave(pi, token, node, arrivals)
        self._pump(pi, arrivals)

    def _run_async_continuation(self, job: Job) -> None:
        """async-continuation 到期：直接执行节点行为（_dispatch_node 不再拆 async）。

        token 在 asyncBefore 拆分时已 open actinst 停等；job 执行 = 行为主体
        （open 复用避免重复记 actinst），完成后继续推进。
        """
        pi = self._instances.get(job.process_instance_id)
        token = pi.executions.get(job.execution_id) if pi is not None else None
        if (
            pi is None
            or pi.is_completed
            or token is None
            or token.state != ExecutionState.ACTIVE
        ):
            self._jobs.pop(job.id, None)
            return
        proc = self._container_of(pi, token)
        node = proc.flow_nodes[job.node_id]
        arrivals = self._dispatch_node(pi, token, node)
        self._pump(pi, arrivals)
        # 宿主（asyncBefore 节点）活动是否仍在等待：仅当行为后 token 仍停在同一
        # 节点且活动未结算（asyncBefore + userTask 组合停等）时边界 timer 继续
        # 有效；已离开 / 停在 join 等待（并行网关无活动等待窗口）即作废
        still_waiting = (
            token.state == ExecutionState.ACTIVE
            and token.activity_id == node.id
            and token.open_activity is not None
            and token.open_activity.end_time is None
            and token.id not in pi.join_arrived(node.id)
        )
        if not still_waiting:
            self._drop_boundary_jobs(pi, node)

    def _run_async_after(self, job: Job) -> None:
        """async-after 到期：执行「离开推进」。

        serviceTask 沿出边离开（多出边 fork）；XOR 此时重新求值条件选路后 take
        —— 行为与离开之间的异步窗口内变量可能已变化，条件以 job 到期时刻为准。
        token 已推进到别处（activity_id != job.node_id）= 过期作业，直接丢弃。
        """
        pi = self._instances.get(job.process_instance_id)
        token = pi.executions.get(job.execution_id) if pi is not None else None
        if (
            pi is None
            or pi.is_completed
            or token is None
            or token.state != ExecutionState.ACTIVE
            or token.activity_id != job.node_id
        ):
            self._jobs.pop(job.id, None)  # token 已推进 -> 过期作业丢弃
            return
        proc = self._container_of(pi, token)
        node = proc.flow_nodes[job.node_id]
        arrivals: List[_Arrival] = []
        if isinstance(node, ServiceTask):
            self._leave(pi, token, node, arrivals)
        elif isinstance(node, ExclusiveGateway):
            chosen = select_exclusive_gateway_flow(
                node, self._outgoing(proc, node), pi.variables
            )
            self._take(pi, token, chosen, arrivals)
        else:  # 防御：异常宿主类型 -> 丢弃（正常解析 + _handle_arrival 校验后不会发生）
            self._jobs.pop(job.id, None)
            return
        self._pump(pi, arrivals)

    def _reschedule_or_remove(self, job: Job) -> None:
        """作业成功后处理：timer-start 按 repeat 续排下一 duedate；其余删除。

        interval 按「计划 duedate 链式 + 周期」续排（执行延迟不累积漂移）；
        cron 从当前时刻求下一未来触发。count 递减到 0 即停排（R3/PT.. = 触发 3 次）。
        """
        if not job.repeat:
            self._jobs.pop(job.id, None)
            return
        rep = dict(job.repeat)
        if rep["kind"] == "interval":
            if rep.get("count") is not None:
                rep["count"] -= 1
                if rep["count"] <= 0:
                    self._jobs.pop(job.id, None)
                    return
                job.repeat = rep
            job.duedate = format_iso(
                parse_iso(job.duedate) + timedelta(seconds=rep["seconds"])
            )
        else:  # cron
            job.duedate = format_iso(next_trigger(rep, parse_iso(_now())))
            job.repeat = rep

    def _degrade_after_failure(self, job: Job) -> None:
        """失败降级：retries-1；未耗尽则按 retry_delay_seconds 顺延 duedate。"""
        job.retries -= 1
        if job.retries > 0:
            job.duedate = format_iso(
                parse_iso(_now()) + timedelta(seconds=job.retry_delay_seconds)
            )
        # retries 耗尽 = 死信：保留记录（可 create_job_query 查看），不再被 acquire

    def _rollback_instance(self, proc_inst_id: str) -> None:
        """把实例内存态回滚到上次同步点（从 RU/HI 重读重建，store 未动无需回写）。"""
        snap = next(
            (s for s in self._store.load_active_instances() if s.id == proc_inst_id),
            None,
        )
        if snap is None:
            return
        for tid in [t.id for t in self._tasks.values() if t.process_instance_id == proc_inst_id]:
            self._tasks.pop(tid, None)
        for jid in [j.id for j in self._jobs.values() if j.process_instance_id == proc_inst_id]:
            self._jobs.pop(jid, None)
        self._instances.pop(proc_inst_id, None)
        self._restore_instance(snap)

    def _persist_job_state(self, job: Job) -> None:
        """作业执行成功/失败后的事务边界同步（store 模式）。"""
        if job.is_definition_level:
            self._sync_timer_start_jobs()
            return
        pi = self._instances.get(job.process_instance_id)
        if pi is not None:
            self._store.save_proc_inst(self._build_snap(pi))
