"""JobExecutor：后台轮询线程（对齐 Camunda JobExecutor 职责，M3 + M7）。

职责：周期性调用 engine.execute_due_jobs()，让到期作业（timer-start /
timer-catch / async-continuation）被自动执行 —— 成功删除/续排、失败重试
顺延、死信停排都由引擎侧语义保证，这里只做「唤醒与节奏」。

设计要点：
- tick() 单步方法：一次立即执行当前到期作业（手动触发 / 测试拨钟后用，
  不依赖线程）。
- start()/shutdown() 管理后台线程；轮询间隔可配（默认 1s）。
- 空闲等待用 Event.wait —— shutdown 即时响应，不留 sleep 尾巴。
- 引擎锁在 execute_due_jobs 内部（RLock），轮询线程与用户命令天然互斥。
- M7：多 JobExecutor / 多进程场景下，JobExecutor 自动分配唯一 lock_owner
  （hostname-pid-uuid8），通过 store CAS lease 跨节点抢锁（详见
  Store.acquire_due_jobs 与 ProcessEngine._execute_due_jobs_db）。无 store
  时仍走单进程内存路径（向后兼容）。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # 仅类型标注，避免循环依赖
    from camunda.engine.process_engine import ProcessEngine

logger = logging.getLogger(__name__)


def _default_lock_owner(name: str) -> str:
    """生成默认 lock_owner：name-pid-hostname-uuid8（保证跨进程唯一）。

    name 放在最前，便于人眼 / 断言识别（hostname 可能含连字符或数字，
    pid 同理）。跨进程唯一性：uuid8 + pid + hostname 一起视为全局唯一；
    最终 CAS 仍然按 owner 字符串比对。
    """
    return f"{name}-{os.getpid()}-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


class JobExecutor:
    """到期作业轮询执行器（每进程一个；与引擎实例绑定）。

    M7：当 engine 持有 Store 时，自动启用 DB CAS lease 抢锁路径，
    lock_owner 默认按 hostname-pid-name-uuid 生成，跨 JobExecutor 实例
    唯一。store=None 时仍走单进程内存路径（向后兼容旧用法）。
    """

    def __init__(
        self,
        engine: "ProcessEngine",
        poll_interval: float = 1.0,
        name: str = "job-executor",
        lock_owner: Optional[str] = None,
        lease_seconds: int = 300,
    ) -> None:
        self._engine = engine
        self._interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._name = name
        self._lease_seconds = lease_seconds
        # lock_owner：调用方可显式指定（便于测试 / 复用同一 owner 标识），
        # 不指定则按 hostname-pid-name-uuid8 自动生成
        self._lock_owner = lock_owner or _default_lock_owner(name)
        self._db_locking_enabled = engine._store is not None

    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def lock_owner(self) -> str:
        """当前 JobExecutor 持有的 DB lock_owner（M7 多实例唯一标识）。"""
        return self._lock_owner

    @property
    def db_locking_enabled(self) -> bool:
        """是否启用 DB CAS lease 抢锁（engine 有 Store + lock_owner 已分配）。"""
        return self._db_locking_enabled

    def tick(self) -> int:
        """单步：立即执行一轮到期作业（返回执行条数）。线程内外皆可用。

        M7：当 DB 抢锁启用时，传入 lock_owner 让 execute_due_jobs 走
        CAS lease 路径；否则走内存路径。
        """
        if self._db_locking_enabled:
            return self._engine.execute_due_jobs(
                lock_owner=self._lock_owner,
                lease_seconds=self._lease_seconds,
            )
        return self._engine.execute_due_jobs()

    def start(self) -> None:
        """启动后台轮询线程（幂等：已在运行则忽略）。"""
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        logger.info(
            "JobExecutor %r 启动（owner=%s, lease=%ds, 轮询间隔 %.2fs, DB抢锁=%s）",
            self._name,
            self._lock_owner,
            self._lease_seconds,
            self._interval,
            self._db_locking_enabled,
        )

    def shutdown(self, timeout: Optional[float] = None) -> None:
        """停止轮询线程并等待退出。

        M7：不主动释放自己持有的 lease —— 让其自然过期，便于模拟崩溃
        恢复测试（其他 JobExecutor 在 lease 过期后可抢到作业）。若需
        立刻释放，调 engine._store.complete_job_cas / reschedule_job_cas。
        """
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("JobExecutor %r 已停止（owner=%s）", self._name, self._lock_owner)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:  # 单轮异常不杀死轮询线程
                logger.exception("JobExecutor 轮询一轮失败")
