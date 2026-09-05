"""engine 包：流程引擎核心（M1 内存版）。

- ProcessEngine      门面：deploy / start_process_instance / complete_task / 查询
- behavior           节点行为分派（start/end/userTask/serviceTask/gateway/flow）
"""

from camunda.engine.process_engine import ProcessEngine

__all__ = ["ProcessEngine"]
