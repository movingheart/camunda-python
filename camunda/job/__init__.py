"""job 包：作业执行器（M3 里程碑交付：Timer / async continuation 轮询执行；
M4-1 扩展 timer 边界事件作业）。

- executor.JobExecutor  后台轮询线程 + tick() 单步（到期作业自动执行/重试/死信）
"""

from camunda.job.executor import JobExecutor

__all__ = ["JobExecutor"]
