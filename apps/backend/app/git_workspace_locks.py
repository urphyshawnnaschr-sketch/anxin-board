"""同项目 Git 工作区操作互斥；不访问 FastAPI 或 SQLite。

Git 连接检查与范围刷新必须使用同一把项目级锁，避免两个操作并发修改同一工作区。
同项目并发操作第二个请求返回 409，不同项目可并行；锁在异常和超时后释放。
同一请求可在上层 critical section 中重入同一项目锁，以组合多个已有 authoritative read seam。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class GitOperationInProgress(RuntimeError):
    """同项目已有其他 Git 工作区操作正在进行。"""


_GUARD = threading.Lock()
_LOCKS: dict[int, threading.RLock] = {}


def _lock_for(project_id: int) -> threading.RLock:
    with _GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


@contextmanager
def project_workspace_lock(
    project_id: int,
    *,
    acquire_timeout: float = 0.0,
) -> Iterator[None]:
    """按项目获取互斥锁；获取失败时抛出 GitOperationInProgress。

    默认非阻塞（acquire_timeout=0.0）。不同线程/请求对同项目仍严格互斥；
    同一线程可重入，以便一个上层事务在调用会再次获取该锁的 authoritative seam 时，
    把锁持续持有到整个 critical section 结束。每次成功 acquire 都由对应 with 释放。
    """
    lock = _lock_for(project_id)
    acquired = lock.acquire(timeout=acquire_timeout)
    if not acquired:
        raise GitOperationInProgress()
    try:
        yield
    finally:
        lock.release()
