"""房间引导。

方案 3.1 只说了创建房间时生成 `room_id` 与 `owner_secret`，没说房间还带
什么。但 4.1 写明"默认日常"、4.4 写明"访客区是每个房间创建时自动生成的
唯一常驻区域"——所以**两个初始区域是房间的固有组成部分，不是 CRUD 的产物**。

因此它们在本模块与房间同时创建，而不是留给 M3 的区域接口去补。这样也
避免了"房间存在但没有任何区域可写"的中间态。

本模块同时承担一个不可回避的职责：**引导与找回**。产品没有注册接口，
主人链接里的 secret 在库中只有哈希，丢了就打印不出来，所以必须有命令行
入口（`python -m app.cli`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database
from .security import hash_token, new_id, new_token

DEFAULT_ROOM_NAME = "我的空间"

DEFAULT_BOARD_NAME = "日常"
GUEST_BOARD_NAME = "访客区"

# 日常区的保留期。方案没有规定初始区域的寿命，这里取"永久"。
#
# 理由是产品红线（0.3：不丢数据）优先：日常区是默认落地页，如果它按 60 天
# 静默归档，用户在不看提醒的情况下会直接失去写入能力。想要生命周期的用户
# 可以自行新建 30/60 天的区域，能力没有任何损失，但默认状态必须是安全的。
DEFAULT_BOARD_RETENTION = 0

# 访客区固定 30 天滑动过期（方案 4.4）。retention 与 expires_at 必须同时
# 满足 DDL 的 CHECK 约束：retention=30 时 expires_at 不得为空。
GUEST_BOARD_RETENTION = 30
SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class RoomBootstrap:
    room_id: str
    room_name: str
    owner_secret: str

    @property
    def pair_path(self) -> str:
        """主人链接的路径部分。

        只返回路径不返回完整 URL：服务端并不知道自己对外被哪个域名访问
        （由 Caddy 决定），拼一个猜出来的域名反而会误导。
        """
        return f"/pair/{self.owner_secret}"


async def bump_rev(conn, room_id: str) -> int:
    """递增房间修订号并返回新值。

    **必须传入写事务里的连接，不能传 Database。** 这是刻意的签名设计：方案
    4.3 要求 `rev` 单调、无空洞，而这个保证完全依赖于"递增与业务写入在同一个
    `BEGIN IMMEDIATE` 事务里"。只要接受 Database，就会诱使调用方另开一次
    写事务——两次事务之间一旦插入回滚，`rev` 就出现空洞。改成只收 conn，
    写错的可能性从"需要小心"变成"根本写不出来"。

    `UPDATE` 后再 `SELECT` 是安全的：单连接 + 全局锁串行化，两者之间不可能
    有别的写入插进来。
    """
    await conn.execute("UPDATE rooms SET rev = rev + 1 WHERE id = ?", (room_id,))
    async with conn.execute("SELECT rev FROM rooms WHERE id = ?", (room_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError(f"递增 rev 失败：房间不存在 {room_id}")
    return int(row[0])


async def count_rooms(db: Database) -> int:
    row = await db.fetchone("SELECT COUNT(*) AS total FROM rooms")
    return int(row["total"]) if row else 0


async def list_rooms(db: Database) -> list[dict]:
    return await db.fetchall("SELECT id, name, rev, created_at FROM rooms ORDER BY created_at ASC")


async def find_room_by_owner_secret(db: Database, secret: str) -> dict | None:
    """按主人 secret 找房间。查的是哈希，明文从不落库也不落日志。"""
    return await db.fetchone(
        "SELECT id, name FROM rooms WHERE owner_secret_hash = ?",
        (hash_token(secret),),
    )


async def create_room(
    db: Database, *, name: str = DEFAULT_ROOM_NAME, now: int | None = None
) -> RoomBootstrap:
    """创建房间及其两个初始区域。全部在一个事务里。"""
    now = int(time.time()) if now is None else now
    room_id = new_id()
    secret = new_token()

    async with db.write() as conn:
        await conn.execute(
            """
            INSERT INTO rooms (id, name, owner_secret_hash, rev, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (room_id, name, hash_token(secret), now),
        )
        await conn.execute(
            """
            INSERT INTO boards
              (id, room_id, name, retention, expires_at, status, is_guest, archived_at, sort_order, created_at)
            VALUES (?, ?, ?, ?, NULL, 'active', 0, NULL, 0, ?)
            """,
            (new_id(), room_id, DEFAULT_BOARD_NAME, DEFAULT_BOARD_RETENTION, now),
        )
        await conn.execute(
            """
            INSERT INTO boards
              (id, room_id, name, retention, expires_at, status, is_guest, archived_at, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', 1, NULL, 1, ?)
            """,
            (
                new_id(),
                room_id,
                GUEST_BOARD_NAME,
                GUEST_BOARD_RETENTION,
                now + GUEST_BOARD_RETENTION * SECONDS_PER_DAY,
                now,
            ),
        )

    return RoomBootstrap(room_id=room_id, room_name=name, owner_secret=secret)


async def rotate_owner_secret(
    db: Database, *, room_id: str, now: int | None = None
) -> str:
    """重置主人链接。返回新的 secret 明文。

    方案 3.1：旧链接立即失效，**已配对设备不受影响**——所以这里只换
    `owner_secret_hash`，一行都不碰 `device_sessions`。
    """
    secret = new_token()
    async with db.write() as conn:
        await conn.execute(
            "UPDATE rooms SET owner_secret_hash = ? WHERE id = ?",
            (hash_token(secret), room_id),
        )
    return secret
