"""人工任务模型（对齐 ACT_RU_TASK 语义）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Task:
    """用户任务运行时实例：等待用户 complete。"""

    id: str
    name: Optional[str] = None
    process_instance_id: str = ""
    execution_id: str = ""          # 持有该任务的 execution
    task_definition_key: str = ""   # BPMN userTask id
    assignee: Optional[str] = None
    candidate_users: List[str] = field(default_factory=list)
    candidate_groups: List[str] = field(default_factory=list)
    create_time: Optional[str] = None
    end_time: Optional[str] = None  # complete 后写入（HI_TASKINST 归档）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "process_instance_id": self.process_instance_id,
            "task_definition_key": self.task_definition_key,
            "assignee": self.assignee,
            "create_time": self.create_time,
            "end_time": self.end_time,
        }
