"""任务存储公共行为契约测试。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from task_store import InMemoryTaskStore


@pytest.fixture
def store():
    return InMemoryTaskStore()


def test_new_task_is_created_in_queued_state(store):
    task_id, task, created = store.create_or_get(
        task_id="task-1",
        imgs=[],
        request_id="request-1",
        sys_user_id="user-1",
    )

    assert created is True
    assert task_id == "task-1"
    assert task["taskState"] == 0
    assert store.get("task-1") == task


def test_same_user_request_returns_original_task(store):
    store.create_or_get(
        task_id="task-a",
        imgs=[],
        request_id="request-shared",
        sys_user_id="user-1",
    )

    task_id, _, created = store.create_or_get(
        task_id="task-b",
        imgs=[],
        request_id="request-shared",
        sys_user_id="user-1",
    )

    assert created is False
    assert task_id == "task-a"
    assert store.get("task-b") is None


def test_same_request_for_different_users_creates_two_tasks(store):
    first = store.create_or_get(
        task_id="task-a",
        imgs=[],
        request_id="request-shared",
        sys_user_id="user-a",
    )
    second = store.create_or_get(
        task_id="task-b",
        imgs=[],
        request_id="request-shared",
        sys_user_id="user-b",
    )

    assert first[2] is True
    assert second[2] is True
    assert store.get("task-a") is not None
    assert store.get("task-b") is not None


def test_same_task_id_returns_original_task(store):
    store.create_or_get(
        task_id="task-shared",
        imgs=[],
        request_id="request-a",
        sys_user_id="user-a",
    )

    task_id, task, created = store.create_or_get(
        task_id="task-shared",
        imgs=[],
        request_id="request-b",
        sys_user_id="user-a",
    )

    assert created is False
    assert task_id == "task-shared"
    assert task["requestId"] == "request-a"
    assert task["sysUserId"] == "user-a"


def test_task_can_only_be_claimed_once(store):
    store.create_or_get(
        task_id="task-1",
        imgs=[],
        request_id="request-1",
        sys_user_id="user-1",
    )

    assert store.claim("task-1") is True
    assert store.claim("task-1") is False
    assert store.get("task-1")["taskState"] == 1


def test_running_task_can_be_completed_with_result(store):
    store.create_or_get(
        task_id="task-1",
        imgs=[],
        request_id="request-1",
        sys_user_id="user-1",
    )
    store.claim("task-1")

    assert store.complete("task-1", {"lvef": 55.0}) is True
    task = store.get("task-1")
    assert task["taskState"] == 2
    assert task["result"] == {"lvef": 55.0}


def test_running_task_can_fail_with_reason(store):
    store.create_or_get(
        task_id="task-1",
        imgs=[],
        request_id="request-1",
        sys_user_id="user-1",
    )
    store.claim("task-1")

    assert store.fail("task-1", "ECG-FM failed") is True
    task = store.get("task-1")
    assert task["taskState"] == 3
    assert task["failedReason"] == "ECG-FM failed"


def test_terminal_task_cannot_be_overwritten(store):
    store.create_or_get(
        task_id="task-1",
        imgs=[],
        request_id="request-1",
        sys_user_id="user-1",
    )
    store.claim("task-1")
    store.complete("task-1", {"lvef": 55.0})

    assert store.complete("task-1", {"lvef": 10.0}) is False
    assert store.fail("task-1", "late failure") is False
    task = store.get("task-1")
    assert task["taskState"] == 2
    assert task["result"] == {"lvef": 55.0}
    assert task["failedReason"] is None


def test_concurrent_same_user_request_creates_only_one_task(store):
    def create(index: int):
        return store.create_or_get(
            task_id=f"task-{index}",
            imgs=[],
            request_id="request-shared",
            sys_user_id="user-1",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(8)))

    created = [result for result in results if result[2]]
    returned_task_ids = {result[0] for result in results}

    assert len(created) == 1
    assert len(returned_task_ids) == 1
