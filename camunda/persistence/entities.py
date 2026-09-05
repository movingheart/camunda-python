"""SQLAlchemy 2.0 ORM 实体：对齐 Camunda ACT_* 表契约。

表契约（对齐 Camunda 7 的 MyBatis 表）：
- ACT_RE_DEPLOYMENT / ACT_RE_PROCDEF   静态定义（M2 简化：资源 xml 直接存 prodef 行）
- ACT_RU_EXECUTION / ACT_RU_TASK /
  ACT_RU_VARIABLE                     运行时瞬时态（RU = running）
- ACT_HI_PROCINST / ACT_HI_ACTINST /
  ACT_HI_TASKINST / ACT_HI_VARINST    历史归档

M2 差异说明（文档化）：
- Camunda 用 bytearray 表存资源 -> M2 直接存 prodef.resource_xml 文本
- Camunda HI_VARINST 每次变更追加版本 -> M2 按实例快照（每实例每变量一行）
- Camunda 变量挂 execution -> M2 引擎变量为实例级，VARIABLE 表挂 proc_inst
"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 静态定义（RE）
# ---------------------------------------------------------------------------
class DeploymentEntity(Base):
    """一次部署（对齐 ACT_RE_DEPLOYMENT）。"""

    __tablename__ = "ACT_RE_DEPLOYMENT"

    id_: Mapped[str] = mapped_column("ID_", String(64), primary_key=True)
    name_: Mapped[str | None] = mapped_column("NAME_", String(255), nullable=True)
    deploy_time_: Mapped[str] = mapped_column("DEPLOY_TIME_", String(32))


class ProcDefEntity(Base):
    """流程定义（对齐 ACT_RE_PROCDEF）。

    Camunda 主键是 ID_，同 KEY_ 可多版本（VERSION_ 递增）。M2 主键 = f"{key}:{version}"。
    """

    __tablename__ = "ACT_RE_PROCDEF"

    id_: Mapped[str] = mapped_column("ID_", String(128), primary_key=True)
    key_: Mapped[str] = mapped_column("KEY_", String(128), index=True)
    name_: Mapped[str | None] = mapped_column("NAME_", String(255), nullable=True)
    version_: Mapped[int] = mapped_column("VERSION_", default=1)
    deployment_id_: Mapped[str] = mapped_column("DEPLOYMENT_ID_", String(64))
    resource_xml_: Mapped[str] = mapped_column("RESOURCE_XML_", Text)
    is_executable_: Mapped[bool] = mapped_column("IS_EXECUTABLE_", default=True)


# ---------------------------------------------------------------------------
# 运行时（RU）
# ---------------------------------------------------------------------------
class ExecutionEntity(Base):
    """执行树节点（对齐 ACT_RU_EXECUTION）。

    只保存 ACTIVE 的 execution（ENDED 的随历史归档，RU 表语义）。
    """

    __tablename__ = "ACT_RU_EXECUTION"

    id_: Mapped[str] = mapped_column("ID_", String(64), primary_key=True)
    process_instance_id_: Mapped[str] = mapped_column("PROC_INST_ID_", String(64), index=True)
    parent_id_: Mapped[str | None] = mapped_column("PARENT_ID_", String(64), nullable=True)
    activity_id_: Mapped[str | None] = mapped_column("ACT_ID_", String(255), nullable=True)
    role_: Mapped[str] = mapped_column("ROLE_", String(16), default="TOKEN")
    # M4-2c4：多实例状态 JSON（容器 total/active/completed/next_index/... 或实例
    # {"index": i}）。Camunda 以 loopCounter 等 ACT_RU_VARIABLE + IS_SCOPE_ 关联
    # 表达；M2 简化：实例级变量 + 此列直存容器状态（崩溃恢复必需）。
    mi_: Mapped[str | None] = mapped_column("MI_", Text, nullable=True)
    # Camunda 还有 IS_CONCURRENT_ / IS_SCOPE_ 等；role 已覆盖 M1 语义


class TaskEntity(Base):
    """待办任务（对齐 ACT_RU_TASK）。"""

    __tablename__ = "ACT_RU_TASK"

    id_: Mapped[str] = mapped_column("ID_", String(64), primary_key=True)
    name_: Mapped[str | None] = mapped_column("NAME_", String(255), nullable=True)
    process_instance_id_: Mapped[str] = mapped_column("PROC_INST_ID_", String(64), index=True)
    execution_id_: Mapped[str] = mapped_column("EXECUTION_ID_", String(64))
    task_definition_key_: Mapped[str] = mapped_column("TASK_DEF_KEY_", String(255))
    assignee_: Mapped[str | None] = mapped_column("ASSIGNEE_", String(255), nullable=True)
    create_time_: Mapped[str] = mapped_column("CREATE_TIME_", String(32))


class VariableEntity(Base):
    """流程变量（对齐 ACT_RU_VARIABLE；M2 实例级，复合主键 proc_inst+name）。"""

    __tablename__ = "ACT_RU_VARIABLE"

    id_: Mapped[str] = mapped_column("ID_", String(96), primary_key=True)
    process_instance_id_: Mapped[str] = mapped_column("PROC_INST_ID_", String(64), index=True)
    name_: Mapped[str] = mapped_column("NAME_", String(255))
    type_: Mapped[str] = mapped_column("TYPE_", String(32))          # Java 类型名
    text_: Mapped[str | None] = mapped_column("TEXT_", Text, nullable=True)  # JSON 序列化


class JobEntity(Base):
    """可调度作业（对齐 ACT_RU_JOB，M3 核心子集）。

    - 实例级 job（timer-catch / async-continuation）：PROC_INST_ID_ 有值，
      随实例 RU 快照全量重写
    - 定义级 job（timer-start）：PROC_INST_ID_ 为 NULL，PROC_DEF_KEY_ + ACT_ID_
      指向流程与 startEvent；部署新版本时整组重建
    - LOCK_OWNER_ / LOCK_EXP_TIME_ 预留多实例抢占（M3 单进程不使用）
    """

    __tablename__ = "ACT_RU_JOB"

    id_: Mapped[str] = mapped_column("ID_", String(64), primary_key=True)
    job_type_: Mapped[str] = mapped_column("TYPE_", String(32))
    process_instance_id_: Mapped[str | None] = mapped_column(
        "PROC_INST_ID_", String(64), nullable=True, index=True
    )
    execution_id_: Mapped[str | None] = mapped_column(
        "EXECUTION_ID_", String(64), nullable=True
    )
    process_definition_key_: Mapped[str | None] = mapped_column(
        "PROC_DEF_KEY_", String(128), nullable=True, index=True
    )
    node_id_: Mapped[str | None] = mapped_column("ACT_ID_", String(255), nullable=True)
    duedate_: Mapped[str] = mapped_column("DUEDATE_", String(32), index=True)
    created_: Mapped[str] = mapped_column("CREATED_", String(32))
    retries_: Mapped[int] = mapped_column("RETRIES_", Integer, default=3)
    repeat_: Mapped[str | None] = mapped_column("REPEAT_", Text, nullable=True)  # JSON
    lock_owner_: Mapped[str | None] = mapped_column(
        "LOCK_OWNER_", String(255), nullable=True
    )
    lock_expire_time_: Mapped[str | None] = mapped_column(
        "LOCK_EXP_TIME_", String(32), nullable=True
    )


# ---------------------------------------------------------------------------
# 历史（HI）
# ---------------------------------------------------------------------------
class HistProcInstEntity(Base):
    """流程实例历史（对齐 ACT_HI_PROCINST）。"""

    __tablename__ = "ACT_HI_PROCINST"

    id_: Mapped[str] = mapped_column("ID_", String(64), primary_key=True)
    process_definition_key_: Mapped[str] = mapped_column("PROC_DEF_KEY_", String(128), index=True)
    business_key_: Mapped[str | None] = mapped_column("BUSINESS_KEY_", String(255), nullable=True)
    state_: Mapped[str] = mapped_column("STATE_", String(16))        # ACTIVE/COMPLETED
    start_time_: Mapped[str] = mapped_column("START_TIME_", String(32))
    end_time_: Mapped[str | None] = mapped_column("END_TIME_", String(32), nullable=True)


class HistActInstEntity(Base):
    """活动实例历史（对齐 ACT_HI_ACTINST）。"""

    __tablename__ = "ACT_HI_ACTINST"

    id_: Mapped[str] = mapped_column("ID_", String(64), primary_key=True)
    process_instance_id_: Mapped[str] = mapped_column("PROC_INST_ID_", String(64), index=True)
    activity_id_: Mapped[str] = mapped_column("ACT_ID_", String(255))
    activity_name_: Mapped[str | None] = mapped_column("ACT_NAME_", String(255), nullable=True)
    execution_id_: Mapped[str] = mapped_column("EXECUTION_ID_", String(64))
    start_time_: Mapped[str] = mapped_column("START_TIME_", String(32))
    end_time_: Mapped[str | None] = mapped_column("END_TIME_", String(32), nullable=True)


class HistTaskInstEntity(Base):
    """任务实例历史（对齐 ACT_HI_TASKINST；M2 在 complete 时写入快照）。"""

    __tablename__ = "ACT_HI_TASKINST"

    id_: Mapped[str] = mapped_column("ID_", String(64), primary_key=True)
    process_instance_id_: Mapped[str] = mapped_column("PROC_INST_ID_", String(64), index=True)
    task_definition_key_: Mapped[str] = mapped_column("TASK_DEF_KEY_", String(255))
    name_: Mapped[str | None] = mapped_column("NAME_", String(255), nullable=True)
    execution_id_: Mapped[str] = mapped_column("EXECUTION_ID_", String(64))
    assignee_: Mapped[str | None] = mapped_column("ASSIGNEE_", String(255), nullable=True)
    create_time_: Mapped[str] = mapped_column("CREATE_TIME_", String(32))
    end_time_: Mapped[str | None] = mapped_column("END_TIME_", String(32), nullable=True)


class HistVarInstEntity(Base):
    """变量历史（对齐 ACT_HI_VARINST；M2 实例快照语义）。"""

    __tablename__ = "ACT_HI_VARINST"

    id_: Mapped[str] = mapped_column("ID_", String(96), primary_key=True)
    process_instance_id_: Mapped[str] = mapped_column("PROC_INST_ID_", String(64), index=True)
    name_: Mapped[str] = mapped_column("NAME_", String(255))
    type_: Mapped[str] = mapped_column("TYPE_", String(32))
    text_: Mapped[str | None] = mapped_column("TEXT_", Text, nullable=True)
