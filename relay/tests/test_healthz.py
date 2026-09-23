"""healthz 与错误信封。

验收标准来自本里程碑的完成定义：/healthz 返回结构化 JSON；删掉数据库文件
重启能自动重建。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app

GIB = 1024 ** 3


def build_client(data_dir: Path, **env: str) -> TestClient:
    values = {"RELAY_DATA_DIR": str(data_dir)}
    values.update(env)
    return TestClient(create_app(load_config(values)))


def test_healthz_reports_ok(tmp_path):
    with build_client(tmp_path / "data") as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["dataDirQuotaBytes"] == 3 * GIB
    assert body["diskFreeBytes"] > 0
    assert body["websocketConnections"] == 0
    # 清理任务属于 M8，接入前必须是 None，不能假装跑过
    assert body["lastCleanupAt"] is None
    assert body["reasons"] == []


def test_healthz_sets_no_store_and_request_id(tmp_path):
    with build_client(tmp_path / "data") as client:
        response = client.get("/healthz")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]


def test_healthz_is_degraded_when_disk_reserve_cannot_be_met(tmp_path):
    with build_client(tmp_path / "data", RELAY_DISK_RESERVE_GB="100000") as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "disk_free_below_reserve" in body["reasons"]


def test_database_is_created_on_first_start(tmp_path):
    data_dir = tmp_path / "data"
    with build_client(data_dir) as client:
        assert client.get("/healthz").status_code == 200

    db_path = data_dir / "relay.db"
    assert db_path.exists()
    connection = sqlite3.connect(db_path)
    try:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()
    assert {"rooms", "boards", "notes", "files"} <= names


def test_database_is_rebuilt_when_file_is_removed(tmp_path):
    data_dir = tmp_path / "data"
    with build_client(data_dir) as client:
        assert client.get("/healthz").status_code == 200

    for suffix in ("", "-wal", "-shm"):
        leftover = Path(str(data_dir / "relay.db") + suffix)
        if leftover.exists():
            leftover.unlink()

    with build_client(data_dir) as client:
        assert client.get("/healthz").status_code == 200


def test_restart_does_not_reapply_migrations(tmp_path):
    data_dir = tmp_path / "data"
    with build_client(data_dir) as client:
        assert client.get("/healthz").status_code == 200
    with build_client(data_dir) as client:
        assert client.get("/healthz").status_code == 200

    connection = sqlite3.connect(data_dir / "relay.db")
    try:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version")]
    finally:
        connection.close()
    assert versions == [1]


def test_unknown_route_returns_error_envelope(tmp_path):
    with build_client(tmp_path / "data") as client:
        response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["requestId"]


def test_method_not_allowed_returns_error_envelope(tmp_path):
    with build_client(tmp_path / "data") as client:
        response = client.post("/healthz")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
