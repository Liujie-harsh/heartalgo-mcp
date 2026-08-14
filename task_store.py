"""任务存储抽象及进程内实现。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Optional, Protocol


class TaskOwnershipError(RuntimeError):
    """taskId 已属于其他用户。"""


@dataclass(frozen=True)
class RecoverableTask:
    """服务重启后需要重新投递到执行队列的持久化任务。"""

    task_id: str
    images: list[Any]


class TaskStore(Protocol):
    """API 依赖的任务存储公共接口。"""

    def create_or_get(
        self,
        task_id: str,
        imgs: list[Any],
        request_id: str = "",
        sys_user_id: str = "",
    ) -> tuple[str, dict, bool]: ...

    def get(self, task_id: str) -> Optional[dict]: ...

    def get_for_user(self, task_id: str, sys_user_id: str) -> Optional[dict]: ...

    def claim(self, task_id: str) -> bool: ...

    def complete(self, task_id: str, result: dict) -> bool: ...

    def fail(self, task_id: str, reason: str) -> bool: ...

    def recover_pending_tasks(self, stale_running_seconds: int) -> list[RecoverableTask]: ...


class InMemoryTaskStore:
    """线程安全的进程内任务存储。

    幂等规则:
      - taskId 全局唯一；重复时返回已有任务。
      - (sysUserId, requestId) 用户内唯一；重复时返回已有任务。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._request_index: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def create_or_get(
        self,
        task_id: str,
        imgs: list[Any],
        request_id: str = "",
        sys_user_id: str = "",
    ) -> tuple[str, dict, bool]:
        """原子创建任务；重复时返回 (已有 taskId, 任务, False)。"""
        request_key = (sys_user_id, request_id)
        with self._lock:
            if request_id and sys_user_id:
                existing_task_id = self._request_index.get(request_key)
                if existing_task_id is not None:
                    return existing_task_id, self._tasks[existing_task_id], False

            existing_task = self._tasks.get(task_id)
            if existing_task is not None:
                if existing_task["sysUserId"] != sys_user_id:
                    raise TaskOwnershipError("task id conflict")
                return task_id, existing_task, False

            task = {
                "taskState": 0,
                "result": None,
                "failedReason": None,
                "imgs": imgs,
                "requestId": request_id,
                "sysUserId": sys_user_id,
                "startedAt": None,
            }
            self._tasks[task_id] = task
            if request_id and sys_user_id:
                self._request_index[request_key] = task_id
            return task_id, task, True

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._tasks.get(task_id)

    def get_for_user(self, task_id: str, sys_user_id: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["sysUserId"] != sys_user_id:
                return None
            return task

    def claim(self, task_id: str) -> bool:
        """原子领取排队任务；只有完成 0→1 的 Worker 返回 True。"""
        with self._lock:
            if self._tasks[task_id]["taskState"] != 0:
                return False
            self._tasks[task_id]["taskState"] = 1
            self._tasks[task_id]["startedAt"] = datetime.now()
            return True

    def complete(self, task_id: str, result: dict) -> bool:
        with self._lock:
            if self._tasks[task_id]["taskState"] != 1:
                return False
            self._tasks[task_id]["taskState"] = 2
            self._tasks[task_id]["result"] = result
            return True

    def fail(self, task_id: str, reason: str) -> bool:
        with self._lock:
            if self._tasks[task_id]["taskState"] not in (0, 1):
                return False
            self._tasks[task_id]["taskState"] = 3
            self._tasks[task_id]["failedReason"] = reason
            return True

    def recover_pending_tasks(self, stale_running_seconds: int) -> list[RecoverableTask]:
        """终止超时运行任务，并返回仍在内存中的排队任务。"""
        if stale_running_seconds < 0:
            raise ValueError("stale_running_seconds 不能小于 0")
        stale_before = datetime.now() - timedelta(seconds=stale_running_seconds)
        interrupted_reason = "任务因算法服务中断而终止，请重新提交"
        with self._lock:
            for task in self._tasks.values():
                started_at = task.get("startedAt")
                if (
                    task["taskState"] == 1
                    and started_at is not None
                    and started_at <= stale_before
                ):
                    task["taskState"] = 3
                    task["failedReason"] = interrupted_reason
            return [
                RecoverableTask(task_id=task_id, images=list(task["imgs"]))
                for task_id, task in self._tasks.items()
                if task["taskState"] == 0
            ]
