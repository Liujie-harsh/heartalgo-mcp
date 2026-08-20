"""服务启动时的任务存储选择测试。"""

import pytest
from fastapi.testclient import TestClient

import main
from database.mysql_task_store import MySQLTaskStore
from task_store import InMemoryTaskStore


def test_build_app_uses_memory_store_by_default(monkeypatch):
    monkeypatch.delenv("TASK_STORE_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
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


def test_cors_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    app = main.build_app(use_fake=True)

    with TestClient(app) as client:
        response = client.options(
            "/heart-algo/task/start",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_only_configured_origin():
    app = main.build_app(
        use_fake=True,
        cors_allowed_origins=["https://app.example"],
    )

    with TestClient(app) as client:
        allowed = client.options(
            "/heart-algo/task/start",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        denied = client.options(
            "/heart-algo/task/start",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert allowed.headers["access-control-allow-origin"] == "https://app.example"
    assert "access-control-allow-origin" not in denied.headers


def test_stale_running_threshold_comes_from_environment(monkeypatch):
    monkeypatch.setenv("TASK_STALE_RUNNING_SECONDS", "123")

    app = main.build_app(use_fake=True)
    try:
        assert app.state.stale_running_seconds == 123
    finally:
        app.state.task_queue.close(wait=True)


def test_cors_rejects_wildcard_origin():
    with pytest.raises(ValueError, match="通配符"):
        main.build_app(use_fake=True, cors_allowed_origins=["*"])
