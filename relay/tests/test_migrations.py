"""数据模型测试：迁移能跑通、约束真的在生效。

这里刻意去撞约束，而不是只检查表存在。DDL 里的 CHECK 和部分唯一索引
是"无人值守时的兜底"，它们失效不会立刻有人发现，所以必须由测试守住。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import load_config
from app.db import Database
from app.migrations import MigrationError, apply_migrations, current_version, discover

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

EXPECTED_TABLES = {
    "schema_version",
    "rooms",
    "devices",
    "device_sessions",
    "boards",
    "notes",
    "files",
    "guest_codes",
}

ROOM = ("r1", "日常", b"\x00", 1)


@pytest.fixture
def config(tmp_path):
    return load_config({"RELAY_DATA_DIR": str(tmp_path / "data")})


@pytest_asyncio.fixture
async def db(config):
    database = Database(config.db_path)
    await database.connect()
    try:
        await apply_migrations(database, MIGRATIONS_DIR)
        yield database
    finally:
        await database.close()


async def _make_room(db: Database) -> None:
    async with db.write() as conn:
        await conn.execute(
            "INSERT INTO rooms (id, name, owner_secret_hash, created_at) VALUES (?, ?, ?, ?)",
            ROOM,
        )


async def _make_board(db: Database, board_id: str, *, is_guest: int = 0) -> None:
    retention = 30 if is_guest else 0
    expires_at = 9_999_999_999 if is_guest else None
    async with db.write() as conn:
        await conn.execute(
            "INSERT INTO boards (id, room_id, name, retention, expires_at, status, is_guest, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'active', ?, 1)",
            (board_id, ROOM[0], board_id, retention, expires_at, is_guest),
        )


async def test_initial_migration_creates_all_tables(db):
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    names = {row["name"] for row in rows}
    assert EXPECTED_TABLES <= names
    assert await current_version(db) == 1


async def test_migrations_are_idempotent(db):
    assert await apply_migrations(db, MIGRATIONS_DIR) == []


async def test_wal_and_foreign_keys_are_enabled(db):
    journal_mode = await db.fetchone("PRAGMA journal_mode")
    assert journal_mode["journal_mode"].lower() == "wal"
    foreign_keys = await db.fetchone("PRAGMA foreign_keys")
    assert next(iter(foreign_keys.values())) == 1


async def test_guest_board_is_unique_per_room(db):
    await _make_room(db)
    await _make_board(db, "guest-1", is_guest=1)
    with pytest.raises(sqlite3.IntegrityError):
        await _make_board(db, "guest-2", is_guest=1)


async def test_normal_boards_are_not_limited_to_one(db):
    await _make_room(db)
    await _make_board(db, "board-a")
    await _make_board(db, "board-b")
    rows = await db.fetchall("SELECT id FROM boards WHERE is_guest = 0")
    assert {row["id"] for row in rows} == {"board-a", "board-b"}


async def test_file_size_upper_bound_is_enforced(db):
    await _make_room(db)
    await _make_board(db, "board-a")
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO files (id, room_id, board_id, display_name, size, sha256,"
                " storage_path, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("f1", ROOM[0], "board-a", "big.bin", 20 * 1024 * 1024 + 1, "x", "/tmp/f1", 1, 1),
            )


async def test_board_retention_and_expiry_must_agree(db):
    await _make_room(db)
    # retention=30 却给 expires_at 置空，违反 CHECK
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO boards (id, room_id, name, retention, expires_at, status, created_at)"
                " VALUES ('bad', ?, '坏区域', 30, NULL, 'active', 1)",
                (ROOM[0],),
            )


async def test_note_mutation_id_is_unique(db):
    await _make_room(db)
    await _make_board(db, "board-a")
    insert = (
        "INSERT INTO notes (id, room_id, board_id, kind, content, mutation_id, created_at, updated_at)"
        " VALUES (?, ?, 'board-a', 'text', '内容', ?, 1, 1)"
    )
    async with db.write() as conn:
        await conn.execute(insert, ("n1", ROOM[0], "mut-1"))
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(insert, ("n2", ROOM[0], "mut-1"))


async def test_foreign_key_violation_is_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO notes (id, room_id, board_id, kind, content, created_at, updated_at)"
                " VALUES ('n9', 'no-such-room', 'no-such-board', 'text', 'x', 1, 1)"
            )


def test_discover_rejects_badly_named_files(tmp_path):
    (tmp_path / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError):
        discover(tmp_path)


def test_discover_rejects_duplicate_versions(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError):
        discover(tmp_path)
