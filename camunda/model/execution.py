"""运行时执行模型：Execution 树 / ProcessInstance / ActivityInstance。

对齐 Camunda ACT_RU_EXECUTION 语义：
- ProcessInstance 是树的根（process_instance_id == 根 execution id 场景下，Camunda
  的 root execution 与 process instance 是两条记录，这里 M1 合并为一条根 Execution）
- 并行网关 fork 时创建子 Execution（parent 停驻成为 scope）
- 每条「活动中的路径」由一条 Execution 携带：activity_id 指向当前所在节点
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查，避免与 task.py 循环（task 不依赖 execution）
    from camunda.model.task import Task


class ExecutionState(str, Enum):
    ACTIVE = "ACTIVE"            # 正等待：用户任务 / join / scope 停驻
    ENDED = "ENDED"              # 已走完（endEvent 或实例结束）


class ProcessInstanceState(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"      # 正常走完 endEvent
    TERMINATED = "TERMINATED"    # 保留字段（M3+ 支持终止）


@dataclass
class Execution:
    """一条执行路径（token 载体）。id 全局唯一。

    role:
    - TOKEN : 活动 token，沿流程推进，可停在 userTask / join 等待
    - SCOPE : 并行 fork 后停驻的父 execution（等待子 TOKEN 完成后再恢复）
    """

    id: str
    process_instance_id: str
    parent_id: Optional[str] = None
    role: str = "TOKEN"  # TOKEN | SCOPE
    # None 表示该 execution 当前不在具体活动上（如 fork 后等待、根 scope）
    activity_id: Optional[str] = None
    state: ExecutionState = ExecutionState.ACTIVE
    # 子 execution（并行网关 fork 产生）
    children: List["Execution"] = field(default_factory=list)
    # 当前停留活动实例（_open_activity 写入，_close_activity 结算时间后清空）
    open_activity: Optional["ActivityInstance"] = None
    # 多实例状态（M4-2c）：None = 非多实例执行。
    # MI 容器（有 "total" 键）：{sequential, total, active, completed, next_index,
    #   items, element_variable, completion_condition} —— parallel 容器挂在转 SCOPE
    #   的宿主 token 上；sequential 容器挂在宿主 token 自身（token 兼作实例载体）。
    # MI 实例（仅并行 child，{"index": i}）：标识该实例序号（收束回报计数用）。
    mi: Optional[Dict[str, Any]] = None

    # ---- 便捷 ----
    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def is_ended(self) -> bool:
        return self.state == ExecutionState.ENDED

    @property
    def is_mi_container(self) -> bool:
        """是否为多实例容器载体（挂 total 键的 MI 状态）。"""
        return self.mi is not None and "total" in self.mi


@dataclass
class ActivityInstance:
    """活动实例历史痕迹（M1 内存版，M2 落 ACT_HI_ACTINST）。

    每次「进入某节点」记录一条，start_time 进入、end_time 离开。
    """

    id: str
    process_instance_id: str
    activity_id: str
    activity_name: Optional[str] = None
    execution_id: str = ""
    start_time: Optional[str] = None
    end_time: Optional[str] = None


@dataclass
class ProcessInstance:
    """流程实例（对齐 ACT_HI_PROCINST 运行时视图）。"""

    id: str
    process_definition_key: str
    business_key: Optional[str] = None
    state: ProcessInstanceState = ProcessInstanceState.ACTIVE
    variables: Dict[str, Any] = field(default_factory=dict)
    root_execution: Optional[Execution] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    # execution id 索引（与树冗余，便于 O(1) 查找）
    executions: Dict[str, Execution] = field(default_factory=dict)
    # 并行网关 join 到达登记：gateway_id -> 已到达的 execution id 列表
    join_arrivals: Dict[str, List[str]] = field(default_factory=dict)
    # 活动痕迹（ACT_HI_ACTINST 内存版）
    activity_history: List[ActivityInstance] = field(default_factory=list)
    # 已完成任务归档（complete 后从 engine 任务表移入，HI_TASKINST 落库）
    completed_tasks: List["Task"] = field(default_factory=list)

    @property
    def is_completed(self) -> bool:
        return self.state != ProcessInstanceState.ACTIVE

    # ---- 并行网关 join 辅助 ----
    def register_join_arrival(self, join_activity_id: str, execution_id: str) -> None:
        self.join_arrivals.setdefault(join_activity_id, []).append(execution_id)

    def join_arrived(self, join_activity_id: str) -> List[str]:
        return self.join_arrivals.get(join_activity_id, [])

    def clear_join_arrivals(self, join_activity_id: str) -> None:
        self.join_arrivals.pop(join_activity_id, None)
