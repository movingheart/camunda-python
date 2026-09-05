"""持久化存取：快照同步 + SQLite/PostgreSQL 支持（M2）。

同步策略（事务边界同步，文档化）：
- M1 引擎在内存推进（事件队列 pump），M2 在**每个命令边界**
  （deploy / start_process_instance / complete_task）把状态全量同步到库。
  崩溃发生在命令中途 => 该命令整体丢失（等价于 Camunda 单命令事务）。
- RU（运行时）表：全量 delete+insert 该实例当前 ACTIVE 状态
  （Camunda 是逐行 update；数据量小，全量重写简单且一致）。
- HI（历史）表：PROCINST upsert 一行；ACTINST / TASKINST / VARINST 全量重写
  该实例当前快照。M2 差异：HI_VARINST 为实例级快照（非每次变更追加版本）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from camunda.common.timers import format_iso, parse_iso
from camunda.model.bpmn import BpmnModel
from camunda.model.job import Job
from camunda.model.variable import java_type_name
from camunda.persistence.entities import (
    Base,
    DeploymentEntity,
    ExecutionEntity,
    HistActInstEntity,
    HistProcInstEntity,
    HistTaskInstEntity,
    HistVarInstEntity,
    JobEntity,
    ProcDefEntity,
    TaskEntity,
    VariableEntity,
)


# ---------------------------------------------------------------------------
# 快照（引擎状态 -> 纯数据）
# ---------------------------------------------------------------------------
@dataclass
class ExecutionSnap:
    id: str
    parent_id: Optional[str]
    activity_id: Optional[str]
    role: str  # TOKEN | SCOPE
    # M4-2c4：多实例状态（容器 dict 或 {"index": i} 实例标识；非 MI 为 None）
    mi: Optional[Dict[str, Any]] = None


@dataclass
class TaskSnap:
    id: str
    name: Optional[str]
    execution_id: str
    task_definition_key: str
    assignee: Optional[str]
    create_time: str
    end_time: Optional[str] = None


@dataclass
class ActivitySnap:
    id: str
    activity_id: str
    activity_name: Optional[str]
    execution_id: str
    start_time: Optional[str]
    end_time: Optional[str]


@dataclass
class JobSnap:
    """实例级作业快照（timer-catch / async-continuation，随实例 RU 全量重写）。"""

    id: str
    job_type: str
    execution_id: Optional[str]
    node_id: Optional[str]
    duedate: str
    created: str
    retries: int = 3
    repeat: Optional[Dict[str, Any]] = None


@dataclass
class ProcInstSnap:
    """一次同步所需的实例状态全集。"""

    id: str
    process_definition_key: str
    business_key: Optional[str]
    state: str  # ACTIVE | COMPLETED
    start_time: str
    end_time: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    executions: List[ExecutionSnap] = field(default_factory=list)
    tasks: List[TaskSnap] = field(default_factory=list)
    jobs: List[JobSnap] = field(default_factory=list)
    activity_history: List[ActivitySnap] = field(default_factory=list)
    completed_tasks: List[TaskSnap] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class Store:
    """ACT 表存取门面。url 形如 sqlite:///abs/path 或 postgresql+psycopg://...

    兼容裸文件路径（如 /tmp/camunda.db）：自动归一化为 sqlite:/// 绝对路径，
    方便测试与命令行直接传 db 路径而不用拼 scheme。
    """

    def __init__(self, url: str) -> None:
        self.url = self._normalize_url(url)
        self._engine = create_engine(self.url, future=True)
        Base.metadata.create_all(self._engine)  # M2：无迁移工具，建表即对齐 schema

    @staticmethod
    def _normalize_url(url: str) -> str:
        """裸路径 -> sqlite:///绝对路径；已带 scheme（xxx://）的 URL 原样放行。

        "sqlite:///" + "/abs/x.db" = "sqlite:////abs/x.db"（4 斜杠 = 绝对路径）。
        """
        if "://" in url:
            return url
        return "sqlite:///" + str(Path(url).expanduser().resolve())

    # ---- session ----
    def session(self) -> Session:
        return Session(self._engine)

    # ------------------------------------------------------------------
    # 部署（RE）
    # ------------------------------------------------------------------
    def save_deployment(self, model: BpmnModel, deploy_time: str) -> str:
        """写部署 + 流程定义行（含原始 xml 供恢复重解析）。返回 deployment id。"""
        deployment_id = uuid.uuid4().hex
        with self.session() as s:
            s.add(
                DeploymentEntity(
                    id_=deployment_id,
                    name_=model.source_name,
                    deploy_time_=deploy_time,
                )
            )
            for proc in model.processes:
                if not proc.is_executable:
                    continue
                # 版本 = 该 key 已有最大版本 + 1（对齐 Camunda 部署新版本语义）
                max_v = s.scalar(
                    select(ProcDefEntity.version_)
                    .where(ProcDefEntity.key_ == proc.id)
                    .order_by(ProcDefEntity.version_.desc())
                    .limit(1)
                )
                version = (max_v or 0) + 1
                s.add(
                    ProcDefEntity(
                        id_=f"{proc.id}:{version}",
                        key_=proc.id,
                        name_=proc.name,
                        version_=version,
                        deployment_id_=deployment_id,
                        resource_xml_=model.source_xml or "",
                        is_executable_=True,
                    )
                )
            s.commit()
        return deployment_id

    def load_proc_defs(self) -> List[Dict[str, Any]]:
        """全部流程定义行（调用方自选版本/重解析 xml）。"""
        with self.session() as s:
            rows = s.execute(select(ProcDefEntity)).scalars().all()
            return [
                {
                    "key": r.key_,
                    "name": r.name_,
                    "version": r.version_,
                    "xml": r.resource_xml_,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # 实例（RU + HI）
    # ------------------------------------------------------------------
    def save_proc_inst(self, snap: ProcInstSnap) -> None:
        """事务边界全量同步一个实例：重写 RU 活跃态 + HI 历史快照。"""
        with self.session() as s:
            # ---- RU：清旧写新 ----
            self._clear_runtime(s, snap.id)
            for ex in snap.executions:
                s.add(
                    ExecutionEntity(
                        id_=ex.id,
                        process_instance_id_=snap.id,
                        parent_id_=ex.parent_id,
                        activity_id_=ex.activity_id,
                        role_=ex.role,
                        mi_=json.dumps(ex.mi, ensure_ascii=False) if ex.mi else None,
                    )
                )
            for t in snap.tasks:
                s.add(
                    TaskEntity(
                        id_=t.id,
                        name_=t.name,
                        process_instance_id_=snap.id,
                        execution_id_=t.execution_id,
                        task_definition_key_=t.task_definition_key,
                        assignee_=t.assignee,
                        create_time_=t.create_time,
                    )
                )
            for name, value in snap.variables.items():
                s.add(
                    VariableEntity(
                        id_=f"{snap.id}:{name}",
                        process_instance_id_=snap.id,
                        name_=name,
                        type_=java_type_name(value),
                        text_=json.dumps(value, ensure_ascii=False),
                    )
                )
            # ---- RU：JOB（实例级作业，如停在 timerCatch / asyncBefore）----
            for j in snap.jobs:
                s.add(
                    JobEntity(
                        id_=j.id,
                        job_type_=j.job_type,
                        process_instance_id_=snap.id,
                        execution_id_=j.execution_id,
                        process_definition_key_=None,
                        node_id_=j.node_id,
                        duedate_=j.duedate,
                        created_=j.created,
                        retries_=j.retries,
                        repeat_=json.dumps(j.repeat, ensure_ascii=False) if j.repeat else None,
                    )
                )

            # ---- HI：PROCINST upsert ----
            s.execute(delete(HistProcInstEntity).where(HistProcInstEntity.id_ == snap.id))
            s.add(
                HistProcInstEntity(
                    id_=snap.id,
                    process_definition_key_=snap.process_definition_key,
                    business_key_=snap.business_key,
                    state_=snap.state,
                    start_time_=snap.start_time,
                    end_time_=snap.end_time,
                )
            )
            # ---- HI：ACTINST 全量 ----
            s.execute(
                delete(HistActInstEntity).where(
                    HistActInstEntity.process_instance_id_ == snap.id
                )
            )
            for a in snap.activity_history:
                s.add(
                    HistActInstEntity(
                        id_=a.id,
                        process_instance_id_=snap.id,
                        activity_id_=a.activity_id,
                        activity_name_=a.activity_name,
                        execution_id_=a.execution_id,
                        start_time_=a.start_time or "",
                        end_time_=a.end_time,
                    )
                )
            # ---- HI：TASKINST（待办无 end + 已办归档带 end_time）----
            s.execute(
                delete(HistTaskInstEntity).where(
                    HistTaskInstEntity.process_instance_id_ == snap.id
                )
            )
            active_task_ids = {t.id for t in snap.tasks}
            for t in snap.completed_tasks + snap.tasks:
                s.add(
                    HistTaskInstEntity(
                        id_=t.id,
                        process_instance_id_=snap.id,
                        task_definition_key_=t.task_definition_key,
                        name_=t.name,
                        execution_id_=t.execution_id,
                        assignee_=t.assignee,
                        create_time_=t.create_time,
                        # 待办任务尚无 end_time（同一次 sync 内既在 active 又在历史，只可能刚完成时状态迁移）
                        end_time_=None if t.id in active_task_ids else t.end_time,
                    )
                )
            # ---- HI：VARINST 实例快照 ----
            s.execute(
                delete(HistVarInstEntity).where(
                    HistVarInstEntity.process_instance_id_ == snap.id
                )
            )
            for name, value in snap.variables.items():
                s.add(
                    HistVarInstEntity(
                        id_=f"{snap.id}:{name}",
                        process_instance_id_=snap.id,
                        name_=name,
                        type_=java_type_name(value),
                        text_=json.dumps(value, ensure_ascii=False),
                    )
                )
            s.commit()

    # ------------------------------------------------------------------
    # 删除（M6：DELETE /process-instance/{id}）
    # ------------------------------------------------------------------
    def delete_proc_inst(self, proc_inst_id: str, end_time: str) -> None:
        """删除运行中实例：清 RU 行，HI_PROCINST 标记 DELETED（历史保留）。

        对齐 Camunda 默认语义（不传 skipHistory 时保留历史）：ACTINST/TASKINST/
        VARINST 历史行不动，仅把实例历史行置 DELETED 并写 end_time，便于
        /history/process-instance 查到被删实例的痕迹。
        """
        with self.session() as s:
            Store._clear_runtime(s, proc_inst_id)
            s.execute(
                update(HistProcInstEntity)
                .where(HistProcInstEntity.id_ == proc_inst_id)
                .values(state_="DELETED", end_time_=end_time)
            )
            s.commit()

    # ------------------------------------------------------------------
    # 恢复读取
    # ------------------------------------------------------------------
    def load_active_instances(self) -> List[ProcInstSnap]:
        """从 RU 恢复所有运行中实例快照（按 PROC_INST_ID 聚合）。"""
        with self.session() as s:
            ex_rows = s.execute(select(ExecutionEntity)).scalars().all()
            task_rows = s.execute(select(TaskEntity)).scalars().all()
            var_rows = s.execute(select(VariableEntity)).scalars().all()
            job_rows = s.execute(
                select(JobEntity).where(JobEntity.process_instance_id_.is_not(None))
            ).scalars().all()
            act_rows = s.execute(
                select(HistActInstEntity).order_by(HistActInstEntity.start_time_)
            ).scalars().all()
            hi_rows = s.execute(select(HistProcInstEntity)).scalars().all()
            # 已归档任务（HI_TASKINST 带 end_time）跨重启保留：HI 表全量重写语义下，
            # 若恢复时不带回来，重启后的下一次 save 会把历史任务抹掉。
            hi_task_rows = s.execute(
                select(HistTaskInstEntity).where(
                    HistTaskInstEntity.end_time_.is_not(None)
                )
            ).scalars().all()

        proc_ids = sorted({r.process_instance_id_ for r in ex_rows} | {r.process_instance_id_ for r in task_rows})
        hi_by_id = {r.id_: r for r in hi_rows}
        result: List[ProcInstSnap] = []
        for pid in proc_ids:
            hi = hi_by_id.get(pid)
            if hi is None:
                continue
            snap = ProcInstSnap(
                id=pid,
                process_definition_key=hi.process_definition_key_,
                business_key=hi.business_key_,
                state=hi.state_,
                start_time=hi.start_time_,
                end_time=hi.end_time_,
            )
            snap.executions = [
                ExecutionSnap(
                    id=r.id_,
                    parent_id=r.parent_id_,
                    activity_id=r.activity_id_,
                    role=r.role_,
                    mi=json.loads(r.mi_) if r.mi_ else None,
                )
                for r in ex_rows
                if r.process_instance_id_ == pid
            ]
            snap.tasks = [
                TaskSnap(
                    id=r.id_,
                    name=r.name_,
                    execution_id=r.execution_id_,
                    task_definition_key=r.task_definition_key_,
                    assignee=r.assignee_,
                    create_time=r.create_time_,
                )
                for r in task_rows
                if r.process_instance_id_ == pid
            ]
            for r in var_rows:
                if r.process_instance_id_ == pid:
                    snap.variables[r.name_] = (
                        json.loads(r.text_) if r.text_ else None
                    )
            snap.jobs = [
                JobSnap(
                    id=r.id_,
                    job_type=r.job_type_,
                    execution_id=r.execution_id_,
                    node_id=r.node_id_,
                    duedate=r.duedate_,
                    created=r.created_,
                    retries=r.retries_,
                    repeat=json.loads(r.repeat_) if r.repeat_ else None,
                )
                for r in job_rows
                if r.process_instance_id_ == pid
            ]
            snap.completed_tasks = [
                TaskSnap(
                    id=r.id_,
                    name=r.name_,
                    execution_id=r.execution_id_,
                    task_definition_key=r.task_definition_key_,
                    assignee=r.assignee_,
                    create_time=r.create_time_,
                    end_time=r.end_time_,
                )
                for r in hi_task_rows
                if r.process_instance_id_ == pid
            ]
            snap.activity_history = [
                ActivitySnap(
                    id=r.id_,
                    activity_id=r.activity_id_,
                    activity_name=r.activity_name_,
                    execution_id=r.execution_id_,
                    start_time=r.start_time_,
                    end_time=r.end_time_,
                )
                for r in act_rows
                if r.process_instance_id_ == pid
            ]
            result.append(snap)
        return result

    @staticmethod
    def _clear_runtime(s: Session, proc_inst_id: str) -> None:
        """清掉该实例的 RU 行（全量重写前置）。"""
        s.execute(
            delete(ExecutionEntity).where(
                ExecutionEntity.process_instance_id_ == proc_inst_id
            )
        )
        s.execute(
            delete(TaskEntity).where(TaskEntity.process_instance_id_ == proc_inst_id)
        )
        s.execute(
            delete(VariableEntity).where(
                VariableEntity.process_instance_id_ == proc_inst_id
            )
        )
        s.execute(
            delete(JobEntity).where(JobEntity.process_instance_id_ == proc_inst_id)
        )

    # ------------------------------------------------------------------
    # 定义级作业（timer-start，不挂实例）
    # ------------------------------------------------------------------
    def save_timer_start_jobs(self, jobs: List[Job]) -> None:
        """全量重写定义级作业（PROC_INST_ID_ IS NULL = timer-start 组）。

        部署新版本 / 恢复后引擎都会整组重算，全量重写最简单且一致。
        """
        with self.session() as s:
            s.execute(delete(JobEntity).where(JobEntity.process_instance_id_.is_(None)))
            for j in jobs:
                s.add(
                    JobEntity(
                        id_=j.id,
                        job_type_=j.job_type,
                        process_instance_id_=None,
                        execution_id_=None,
                        process_definition_key_=j.process_definition_key,
                        node_id_=j.node_id,
                        duedate_=j.duedate,
                        created_=j.created,
                        retries_=j.retries,
                        repeat_=json.dumps(j.repeat, ensure_ascii=False) if j.repeat else None,
                    )
                )
            s.commit()

    def load_timer_start_jobs(self) -> List[Job]:
        """读回全部定义级作业（from_database 恢复 timer start 用）。"""
        with self.session() as s:
            rows = s.execute(
                select(JobEntity).where(JobEntity.process_instance_id_.is_(None))
            ).scalars().all()
        return [
            Job(
                id=r.id_,
                job_type=r.job_type_,
                duedate=r.duedate_,
                created=r.created_,
                process_instance_id=None,
                process_definition_key=r.process_definition_key_,
                node_id=r.node_id_,
                retries=r.retries_,
                repeat=json.loads(r.repeat_) if r.repeat_ else None,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # M7：多 JobExecutor 抢锁原语（CAS lease 模式，对齐 Camunda Job Acquisition
    # Row Lock 的简化版：单条 UPDATE CAS 替代行锁）
    # ------------------------------------------------------------------
    def acquire_due_jobs(
        self,
        lock_owner: str,
        lease_seconds: int,
        due_before: str,
        batch_size: int,
    ) -> List[Job]:
        """抢一批 due job（CAS lease）。

        步骤：
        1. SELECT 候选 ID（按 duedate 升序 + retries>0 + 未持锁/锁已过期，
           limit = batch_size）
        2. 逐条 UPDATE ... WHERE id=:id AND (LOCK_OWNER_ IS NULL OR
           LOCK_EXP_TIME_ < :due_before) SET LOCK_OWNER_=:owner,
           LOCK_EXP_TIME_=:lease_until
           —— affected_rows > 0 即抢到
        3. 再 SELECT WHERE LOCK_OWNER_=:owner AND LOCK_EXP_TIME_ > :due_before
           取详情（防御：步骤 2 抢到后被另一个并发抢走的情况几乎不可能，
           但做最终一致性确认）

        返回：抢到的 Job 列表（带 lock_owner / lock_expire_time 已填充）。
        抢不到返回 []。
        """
        lease_until = format_iso(parse_iso(due_before) + timedelta(seconds=lease_seconds))
        with self.session() as s:
            # 1. 候选 ID（避开 SQL 方言差异：SQLite/PG 的 UPDATE LIMIT 语法不同）
            cand_ids = [
                r[0]
                for r in s.execute(
                    select(JobEntity.id_)
                .where(
                    JobEntity.duedate_ <= due_before,
                    JobEntity.retries_ > 0,
                )
                .where(
                    (JobEntity.lock_owner_.is_(None))
                    | (JobEntity.lock_expire_time_ < due_before)
                )
                .order_by(JobEntity.duedate_)
                .limit(batch_size)
                ).all()
            ]
            if not cand_ids:
                return []
            # 2. 逐条 CAS UPDATE
            for jid in cand_ids:
                res = s.execute(
                    update(JobEntity)
                    .where(
                        JobEntity.id_ == jid,
                    )
                    .where(
                        (JobEntity.lock_owner_.is_(None))
                        | (JobEntity.lock_expire_time_ < due_before)
                    )
                    .values(
                        lock_owner_=lock_owner,
                        lock_expire_time_=lease_until,
                    )
                )
                if res.rowcount == 0:
                    continue  # 被并发抢走
            s.commit()
            # 3. 取详情（owner + 未过期）
            rows = (
                s.execute(
                    select(JobEntity).where(
                        JobEntity.lock_owner_ == lock_owner,
                        JobEntity.lock_expire_time_ > due_before,
                        JobEntity.id_.in_(cand_ids),
                    )
                )
                .scalars()
                .all()
            )
            return [_row_to_job(r) for r in rows]

    def complete_job_cas(self, job_id: str, lock_owner: str) -> bool:
        """CAS 删除已成功执行的 job（防御：非 owner 不删）。

        用于 timer-catch / async / async-after 的一次性作业；timer-start
        按 repeat 续排请用 reschedule_job_cas。
        """
        with self.session() as s:
            res = s.execute(
                delete(JobEntity).where(
                    JobEntity.id_ == job_id,
                    JobEntity.lock_owner_ == lock_owner,
                )
            )
            s.commit()
            return res.rowcount > 0

    def reschedule_job_cas(
        self,
        job_id: str,
        lock_owner: str,
        new_due: str,
        new_retries: int,
        clear_lock: bool = True,
    ) -> bool:
        """CAS 更新 duedate + retries（按 repeat 续排 / 失败降级顺延）。

    clear_lock=True（默认）：续排后清空 LOCK_OWNER_ / LOCK_EXP_TIME_，
    让下一轮由任何 JobExecutor 抢到（对齐 Camunda：作业回到「可被获取」态）。
    clear_lock=False：保留锁（用于同步续约场景，调用方需自己管理 lease）。
    """
        values: Dict[str, Any] = {
            "duedate_": new_due,
            "retries_": new_retries,
        }
        if clear_lock:
            values["lock_owner_"] = None
            values["lock_expire_time_"] = None
        with self.session() as s:
            res = s.execute(
                update(JobEntity)
                .where(JobEntity.id_ == job_id, JobEntity.lock_owner_ == lock_owner)
                .values(**values)
            )
            s.commit()
            return res.rowcount > 0

    def extend_lock(
        self,
        job_id: str,
        lock_owner: str,
        lease_seconds: int,
        due_before: str,
    ) -> bool:
        """CAS 续约：把 lease 延后（用于长作业执行期间）。

        续约失败（owner 已变更）= 锁已被别的 JobExecutor 接管，当前执行应
        中止提交（防御：执行结果 CAS 也会失败，形成闭环保护）。
        """
        new_until = format_iso(parse_iso(due_before) + timedelta(seconds=lease_seconds))
        with self.session() as s:
            res = s.execute(
                update(JobEntity)
                .where(JobEntity.id_ == job_id, JobEntity.lock_owner_ == lock_owner)
                .values(lock_expire_time_=new_until)
            )
            s.commit()
            return res.rowcount > 0

    def list_locks(self, lock_owner: Optional[str] = None) -> List[Dict[str, Any]]:
        """查看当前持锁情况（调试 + 监控用）。

        lock_owner=None 返回全部带锁的作业；指定 owner 时只返回该 owner 的。
        """
        with self.session() as s:
            stmt = select(JobEntity).where(JobEntity.lock_owner_.is_not(None))
            if lock_owner is not None:
                stmt = stmt.where(JobEntity.lock_owner_ == lock_owner)
            rows = s.execute(stmt.order_by(JobEntity.duedate_)).scalars().all()
        return [
            {
                "id": r.id_,
                "lock_owner": r.lock_owner_,
                "lock_expire_time": r.lock_expire_time_,
                "duedate": r.duedate_,
                "job_type": r.job_type_,
                "retries": r.retries_,
                "process_instance_id": r.process_instance_id_,
                "node_id": r.node_id_,
            }
            for r in rows
        ]


def _row_to_job(r: Any) -> Job:
    """JobEntity 行 -> Job 模型（统一转换器，给 acquire_due_jobs 用）。"""
    return Job(
        id=r.id_,
        job_type=r.job_type_,
        duedate=r.duedate_,
        created=r.created_,
        process_instance_id=r.process_instance_id_,
        execution_id=r.execution_id_,
        process_definition_key=r.process_definition_key_,
        node_id=r.node_id_,
        retries=r.retries_,
        repeat=json.loads(r.repeat_) if r.repeat_ else None,
        lock_owner=r.lock_owner_,
        lock_expire_time=r.lock_expire_time_,
    )
