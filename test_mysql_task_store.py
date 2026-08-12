"""MySQLTaskStore 的数据库行为契约。

默认使用内存 SQLite 快速验证持久化边界；设置 TEST_DATABASE_URL 后，
同一组契约会额外在专用 MySQL 测试库上执行。
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from database.mysql_task_store import MySQLTaskStore
from task_models import ImgItem
from task_store import TaskOwnershipError


SCHEMA = """
CREATE TABLE algorithm_task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id VARCHAR(128) NOT NULL UNIQUE,
  request_id VARCHAR(128) NOT NULL,
  sys_user_id VARCHAR(128) NOT NULL,
  task_state INTEGER NOT NULL DEFAULT 0,
  failed_reason TEXT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  started_at DATETIME NULL,
  finished_at DATETIME NULL,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (sys_user_id, request_id)
);
CREATE TABLE algorithm_input (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_db_id INTEGER NOT NULL,
  input_type VARCHAR(32) NOT NULL,
  input_id VARCHAR(128) NOT NULL,
  input_path TEXT NOT NULL,
  dcm_type VARCHAR(64) NULL,
  input_state INTEGER NOT NULL DEFAULT 0,
  failed_reason TEXT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (task_db_id, input_type, input_id)
);
CREATE TABLE algorithm_report (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_db_id INTEGER NOT NULL,
  input_db_id INTEGER NULL,
  execution_id INTEGER NULL,
  report_id VARCHAR(128) NOT NULL UNIQUE,
  report_type VARCHAR(32) NOT NULL,
  report_result TEXT NOT NULL,
  roi_data TEXT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _images() -> list[ImgItem]:
    return [
        ImgItem(imgId="dcm-1", imgPath="C:/data/a.dcm", imgType="CARDIAC_ULTRASOUND", dcmType="A4C"),
        ImgItem(imgId="ecg-1", imgPath="C:/data/a.xml", imgType="ECG"),
    ]


def _outcome(task_id: str) -> dict:
    return {
        "reports": [
            {
                "reportId": f"{task_id}:dcm-1:measurement",
                "reportType": "CU-SUB",
                "reportResult": {"dcmId": "dcm-1", "measurements": {"rvbase": {"value": 20.0}}},
                "inputId": "dcm-1",
                "roiData": [{"roiType": "RVBase", "points": [{"xPos": 1, "yPos": 2}]}],
            },
            {
                "reportId": f"{task_id}:ecg-1:ecg",
                "reportType": "ECG",
                "reportResult": {"ecgId": "ecg-1", "predictions": []},
                "inputId": "ecg-1",
                "roiData": None,
            },
        ],
        "cardiacUltrasound": [{
            "dcmId": "dcm-1",
            "dcmPath": "C:/data/a.dcm",
            "reportId": f"{task_id}:dcm-1:measurement",
            "rois": [{"roiType": "RVBase", "points": [{"xPos": 1, "yPos": 2}]}],
        }],
        "ecg": [{"ecgId": "ecg-1", "ecgPath": "C:/data/a.xml", "reportId": f"{task_id}:ecg-1:ecg"}],
    }


@pytest.fixture(params=["sqlite", "mysql"])
def store_and_engine(request):
    if request.param == "mysql":
        database_url = os.environ.get("TEST_DATABASE_URL")
        if not database_url:
            pytest.skip("未设置 TEST_DATABASE_URL，跳过真实 MySQL 集成测试")
        engine = create_engine(database_url, pool_pre_ping=True, future=True)
        prefix = f"codex_{uuid4().hex}_"
        store = MySQLTaskStore(engine=engine)
        yield store, engine, prefix
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM algorithm_task WHERE task_id LIKE :prefix"), {"prefix": f"{prefix}%"})
        engine.dispose()
        return

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.exec_driver_sql(statement)
    yield MySQLTaskStore(engine=engine), engine, ""
    engine.dispose()


def test_create_is_persistent_and_scoped_to_owner(store_and_engine):
    store, engine, prefix = store_and_engine
    task_id = f"{prefix}task-create"
    returned_id, task, created = store.create_or_get(task_id, _images(), f"{prefix}request-create", "user-a")

    restarted_store = MySQLTaskStore(engine=engine)
    persisted = restarted_store.get_for_user(task_id, "user-a")

    assert (returned_id, created, task["taskState"]) == (task_id, True, 0)
    assert persisted is not None
    assert [item.imgId for item in persisted["imgs"]] == ["dcm-1", "ecg-1"]
    assert restarted_store.get_for_user(task_id, "user-b") is None


def test_same_user_request_is_idempotent(store_and_engine):
    store, _, prefix = store_and_engine
    request_id = f"{prefix}request-idempotent"
    first_id = f"{prefix}task-first"
    second_id = f"{prefix}task-second"
    store.create_or_get(first_id, _images(), request_id, "user-a")

    returned_id, _, created = store.create_or_get(second_id, _images(), request_id, "user-a")

    assert returned_id == first_id
    assert created is False
    assert store.get(second_id) is None


def test_task_id_cannot_be_reused_by_another_owner(store_and_engine):
    store, _, prefix = store_and_engine
    task_id = f"{prefix}task-owned"
    store.create_or_get(task_id, _images(), f"{prefix}request-a", "user-a")

    with pytest.raises(TaskOwnershipError):
        store.create_or_get(task_id, _images(), f"{prefix}request-b", "user-b")


def test_claim_complete_and_reload_reports(store_and_engine):
    store, engine, prefix = store_and_engine
    task_id = f"{prefix}task-complete"
    store.create_or_get(task_id, _images(), f"{prefix}request-complete", "user-a")

    assert store.claim(task_id) is True
    assert store.claim(task_id) is False
    assert store.complete(task_id, _outcome(task_id)) is True

    persisted = MySQLTaskStore(engine=engine).get_for_user(task_id, "user-a")
    assert persisted["taskState"] == 2
    assert persisted["result"] == _outcome(task_id)


def test_failure_is_persistent_and_terminal(store_and_engine):
    store, engine, prefix = store_and_engine
    task_id = f"{prefix}task-failure"
    store.create_or_get(task_id, _images(), f"{prefix}request-failure", "user-a")
    store.claim(task_id)

    assert store.fail(task_id, "runner failed") is True
    assert store.complete(task_id, _outcome(task_id)) is False

    persisted = MySQLTaskStore(engine=engine).get(task_id)
    assert persisted["taskState"] == 3
    assert persisted["failedReason"] == "runner failed"

