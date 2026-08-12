"""服务启动时的任务存储选择测试。"""

import pytest

import main
from database.mysql_task_store import MySQLTaskStore
from task_store import InMemoryTaskStore


def test_build_app_uses_memory_store_by_default():
    app = main.build_app(use_fake=True)
    try:
        assert isinstance(app.state.store, InMemoryTaskStore)
    finally:
        app.state.task_queue.close(wait=True)


def test_build_app_uses_mysql_store_when_selected():
    app = main.build_app(
        use_fake=True,
        task_store_backend="mysql",
        database_url="sqlite+pysqlite:///:memory:",
    )
    try:
        assert isinstance(app.state.store, MySQLTaskStore)
    finally:
        app.state.task_queue.close(wait=True)
        app.state.store.close()


def test_build_app_rejects_mysql_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        main.build_app(use_fake=True, task_store_backend="mysql")

