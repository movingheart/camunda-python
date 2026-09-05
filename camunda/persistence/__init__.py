"""persistence 包：SQLAlchemy 2.0 持久层（M2 里程碑交付）。

- entities.py   ACT_RE_*/ACT_RU_*/ACT_HI_* ORM 实体（对齐 Camunda 表契约）
- store.py      快照同步 + 存取门面（SQLite/PostgreSQL url 均可）
"""

from camunda.persistence.store import (
    ActivitySnap,
    ExecutionSnap,
    ProcInstSnap,
    Store,
    TaskSnap,
)

__all__ = [
    "Store",
    "ProcInstSnap",
    "ExecutionSnap",
    "TaskSnap",
    "ActivitySnap",
]
