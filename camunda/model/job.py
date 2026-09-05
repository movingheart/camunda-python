"""可调度作业模型（对齐 ACT_RU_JOB 语义，M3）。

Camunda JobEntity 关键字段：ID_ / TYPE_ / DUEDATE_ / RETRIES_ / LOCK_OWNER_ /
LOCK_EXP_TIME_ / EXECUTION_ID_ / PROCESS_INSTANCE_ID_ / PROCESS_DEFINITION_ID_ /
ACTIVITY_ID_。M3 保留核心子集。

job_type（对齐 Camunda Job.TYPE_ 取值语义）：
- "timer-catch"        : token 停在 intermediateCatchEvent(timer)，duedate 到期继续流转
- "timer-start"        : 定义级作业（无 process_instance），到点启动流程实例；
                          timerCycle 触发后按 repeat 续排下一个 duedate
- "timer-boundary"     : timer 边界事件（M4-1 中断式；M4-2b4 起 cancelActivity=false
                          非中断式 = 触发不取消宿主、spawn 并发线）。宿主活动等待期内
                          到点触发，中断式取消宿主并让 token 改走边界事件出边
- "timer-event-start"  : 事件子流程的 timer start 订阅（M4-2b3，实例级，execution_id
                          = 宿主 scope，activity_id = 订阅容器 subProcess id，None=
                          流程级=根 Process 容器）。宿主 scope 激活期单发，到点触发
                          中断式（取消宿主）或非中断式（并行 spawn）事件子流程
- "async-continuation" : camunda:asyncBefore 拆分出的「节点行为执行」作业
                          （Camunda 里即 async continuation job）
- "async-after"        : camunda:asyncAfter 拆分出的「离开推进」作业（M4-1；
                          serviceTask/XOR 行为完成后异步流转，XOR 离开时重求值条件）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 默认重试次数与失败延迟（对齐 Camunda 默认行为；M3 不解析 failedJobRetryTimeCycle，
# retry 间隔固定可配。文档化差异）
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 5.0


@dataclass
class Job:
    """一条待执行作业。"""

    id: str
    job_type: str  # timer-catch | timer-start | async-continuation
    duedate: str  # 定长 ISO "%Y-%m-%dT%H:%M:%S"（与 clock.now() 同格式，可字典序比较）
    created: str
    # 实例级（timer-catch / async-continuation）
    process_instance_id: Optional[str] = None
    execution_id: Optional[str] = None
    # 定义级（timer-start）：指向 process key；node_id 为 startEvent id
    process_definition_key: Optional[str] = None
    node_id: Optional[str] = None
    # timer-event-start：订阅容器 subProcess id（None = 流程级/根 Process 容器）；
    # 与 execution_id（宿主 scope）共同唯一定位「哪个容器上的订阅」，撤销精确匹配
    activity_id: Optional[str] = None
    # 重试策略
    retries: int = DEFAULT_MAX_RETRIES
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS
    # timer-start cycle 续排参数（{"kind": "interval"|"cron", ...}，见 common/timers）
    repeat: Optional[Dict[str, Any]] = None
    # 抢占锁（多实例 JobExecutor 预留；M3 单进程不使用）
    lock_owner: Optional[str] = None
    lock_expire_time: Optional[str] = None

    @property
    def is_definition_level(self) -> bool:
        """定义级作业（timer-start）不挂在任何实例上。"""
        return self.process_instance_id is None

    def is_due(self, now: str) -> bool:
        return self.duedate <= now

    def is_dead(self) -> bool:
        return self.retries <= 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_type": self.job_type,
            "process_instance_id": self.process_instance_id,
            "execution_id": self.execution_id,
            "process_definition_key": self.process_definition_key,
            "node_id": self.node_id,
            "activity_id": self.activity_id,
            "duedate": self.duedate,
            "created": self.created,
            "retries": self.retries,
            "repeat": self.repeat,
            "lock_owner": self.lock_owner,
            "lock_expire_time": self.lock_expire_time,
        }
