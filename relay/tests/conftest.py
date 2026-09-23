"""测试基建。

M2 起测试需要"先有房间，再起服务"这个顺序——真实流程也是如此
（`python -m app.cli init` 之后才启动 systemd 服务）。所以这里提供一个
在启动客户端之前把数据准备好的环境对象。

关于 Cookie 的一个必要妥协：`httpx` 的 cookie jar 遵守 Secure 语义，
**不会**把 `Secure` Cookie 发回 `http://testserver`（实测 httpx 0.28.1 /
Python 3.13）。所以走完整会话流程的测试默认把 `RELAY_COOKIE_SECURE` 设为
0——这与本地开发的实际配置一致。生产配置下的 Cookie 属性由一个专门的
测试直接断言响应头，不经过 cookie jar，因此不受这个妥协影响。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.db import Database
from app.identity import SESSION_COOKIE
from app.main import create_app
from app.migrations import apply_migrations
from app.rooms import RoomBootstrap, create_room
from app.security import hash_token, new_token

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# 默认关闭 Secure，理由见模块说明。
DEFAULT_ENV = {"RELAY_COOKIE_SECURE": "0"}


def _with_db(data_dir: Path, action):
    """在独立事件循环里连一次库、跑完迁移、执行 action。

    `action` 是 async 函数，返回什么就带回什么。每次调用都独立开关连接，
    不与 TestClient 的 lifespan 共享——两个连接读写同一个 SQLite 文件是
    WAL 的正常用法。
    """

    async def runner():
        cfg = load_config({"RELAY_DATA_DIR": str(data_dir)})
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        await db.connect()
        await apply_migrations(db, MIGRATIONS_DIR)
        try:
            return await action(db, cfg)
        finally:
            await db.close()

    return asyncio.run(runner())


@dataclass
class GuestDevice:
    device_id: str
    token: str


@dataclass
class RelayEnv:
    data_dir: Path
    rooms: list[RoomBootstrap] = field(default_factory=list)

    @property
    def room(self) -> RoomBootstrap:
        assert self.rooms, "还没有创建房间"
        return self.rooms[0]

    def add_room(self, name: str = "测试房间") -> RoomBootstrap:
        bootstrap = _with_db(self.data_dir, lambda db, _cfg: create_room(db, name=name))
        self.rooms.append(bootstrap)
        return bootstrap

    def add_guest_device(
        self, *, room_id: str | None = None, name: str = "访客设备"
    ) -> GuestDevice:
        """直接插入一台访客设备，返回它的 id 与会话令牌明文。

        访客码整套流程属于 M9；M2 只需要一台"已配对但不是主人"的设备来
        验证权限边界（403 而不是 401 或 404）。
        """
        target_room = room_id or self.room.room_id
        token = new_token()
        device_id = f"guest-{token[:8]}"

        async def insert(db: Database, _cfg):
            now = int(time.time())
            async with db.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO devices (id, room_id, name, ua, role, revoked_at, last_seen_at, created_at)
                    VALUES (?, ?, ?, NULL, 'guest', NULL, ?, ?)
                    """,
                    (device_id, target_room, name, now, now),
                )
                await conn.execute(
                    """
                    INSERT INTO device_sessions (id, device_id, token_hash, expires_at, revoked_at, created_at)
                    VALUES (?, ?, ?, NULL, NULL, ?)
                    """,
                    (f"session-{token[:8]}", device_id, hash_token(token), now),
                )

        _with_db(self.data_dir, insert)
        return GuestDevice(device_id=device_id, token=token)

    def client(self, **env: str) -> TestClient:
        values = {"RELAY_DATA_DIR": str(self.data_dir)}
        values.update(DEFAULT_ENV)
        values.update(env)
        return TestClient(create_app(load_config(values)), follow_redirects=False)

    def cookie_client(self, token: str, **env: str) -> TestClient:
        values = {"RELAY_DATA_DIR": str(self.data_dir)}
        values.update(DEFAULT_ENV)
        values.update(env)
        return TestClient(
            create_app(load_config(values)),
            follow_redirects=False,
            cookies={SESSION_COOKIE: token},
        )

    def read(self, sql: str, params: tuple = ()) -> list[dict]:
        return _with_db(self.data_dir, lambda db, _cfg: db.fetchall(sql, params))

    async def _execute(self, db: Database, _cfg, sql: str, params: tuple) -> None:
        async with db.write() as conn:
            cursor = await conn.execute(sql, params)
            await cursor.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        """直接改库，用于把状态摆到某个特定起点（例如把 last_seen_at 压旧）。"""
        _with_db(self.data_dir, lambda db, cfg: self._execute(db, cfg, sql, params))

    def raw_connect(self):
        """给需要绕过应用层的断言用（例如检查库里的确没有明文 secret）。

        返回一个已经 connect 好的 sqlite3 连接；调用方负责 close。
        """
        import sqlite3

        return sqlite3.connect(self.data_dir / "relay.db")


@pytest.fixture
def relay_env(tmp_path: Path) -> RelayEnv:
    return RelayEnv(data_dir=tmp_path / "data")
