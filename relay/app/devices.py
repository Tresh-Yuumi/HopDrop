"""设备与配对的存取。

设备名是用户可见字段，方案 12 的主防线是 HTML 转义，所以这里**不做**
任何"清洗"——存原样，由渲染层转义。这里只拒绝结构性问题（空、超长、
控制字符），因为那些不是转义能解决的。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database
from .errors import AppError
from .security import hash_token, new_id, new_token

DEVICE_NAME_MAX = 64

# UA 只用于猜默认设备名和排查，没必要完整存下来。
UA_MAX = 512

_DEVICE_COLUMNS = """
  id, name, role, ua, revoked_at, last_seen_at, created_at
"""


@dataclass(frozen=True)
class Pairing:
    """一次成功配对的结果。`session_token` 是明文，只在本次响应里用一次。"""

    device_id: str
    session_id: str
    session_token: str


def guess_device_name(ua: str | None) -> str:
    """从 User-Agent 猜一个默认设备名。

    顺序有讲究：HarmonyOS 的 UA 里带 `Android`，麒麟的 UA 里带 `Linux`，
    所以更具体的判断必须排在更宽泛的前面。
    """
    if not ua:
        return "未知设备"
    low = ua.lower()
    if "harmony" in low or "openharmony" in low:
        return "HarmonyOS 设备"
    if "kylin" in low or "qaxbrowser" in low:
        return "银河麒麟电脑"
    if "android" in low:
        return "Android 设备"
    if "iphone" in low or "ipad" in low or "ios" in low:
        return "iOS 设备"
    if "windows" in low:
        return "Windows 电脑"
    if "macintosh" in low or "mac os" in low:
        return "Mac 电脑"
    if "linux" in low:
        return "Linux 设备"
    return "新设备"


def normalize_device_name(raw: object) -> str:
    """校验并规整设备名。不合法时抛 400。"""
    if not isinstance(raw, str):
        raise AppError.bad_request("设备名必须是字符串", "invalid_device_name")
    name = raw.strip()
    if not name:
        raise AppError.bad_request("设备名不能为空", "invalid_device_name")
    if len(name) > DEVICE_NAME_MAX:
        raise AppError.bad_request(
            f"设备名不能超过 {DEVICE_NAME_MAX} 个字符", "invalid_device_name"
        )
    if any(ch < " " or ch == "\x7f" for ch in name):
        raise AppError.bad_request("设备名不能包含控制字符（含换行和制表符）", "invalid_device_name")
    return name


async def list_devices(db: Database, *, room_id: str) -> list[dict]:
    """房间内的有效设备。已撤销的不再列出——设备列表的用途是"管理还能用
    哪些设备"，把墓碑混在里面只会让界面变复杂。"""
    # 排序用 rowid 而不是 id 做次级键：created_at 只有秒级精度，同一秒内配对
    # 的两台设备会并列，而 id 是随机 uuid——那样列表顺序会随机的。
    # rowid 是插入顺序，稳定且符合"按配对时间排列"的本意。
    return await db.fetchall(
        f"""
        SELECT {_DEVICE_COLUMNS}
        FROM devices
        WHERE room_id = ? AND revoked_at IS NULL
        ORDER BY created_at ASC, rowid ASC
        """,
        (room_id,),
    )


async def create_owner_device(
    db: Database,
    *,
    room_id: str,
    ua: str | None,
    name: str | None = None,
    now: int | None = None,
) -> Pairing:
    """创建主人设备及其长期会话，一次事务完成。

    会话 `expires_at` 为 NULL：方案 3.2 规定主人会话不设短期自动过期，
    退出方式是撤销设备或主动登出。
    """
    now = int(time.time()) if now is None else now
    device_id = new_id()
    session_id = new_id()
    token = new_token()
    resolved_name = normalize_device_name(name) if name is not None else guess_device_name(ua)
    ua_value = ua[:UA_MAX] if ua else None

    async with db.write() as conn:
        await conn.execute(
            """
            INSERT INTO devices (id, room_id, name, ua, role, revoked_at, last_seen_at, created_at)
            VALUES (?, ?, ?, ?, 'owner', NULL, ?, ?)
            """,
            (device_id, room_id, resolved_name, ua_value, now, now),
        )
        await conn.execute(
            """
            INSERT INTO device_sessions (id, device_id, token_hash, expires_at, revoked_at, created_at)
            VALUES (?, ?, ?, NULL, NULL, ?)
            """,
            (session_id, device_id, hash_token(token), now),
        )

    return Pairing(device_id=device_id, session_id=session_id, session_token=token)


async def rename_device(db: Database, *, room_id: str, device_id: str, name: str) -> bool:
    """改名。返回 False 表示该设备不属于本房间或已撤销——调用方据此返回 404。"""
    resolved = normalize_device_name(name)
    now = int(time.time())
    async with db.write() as conn:
        cursor = await conn.execute(
            """
            UPDATE devices SET name = ?
            WHERE id = ? AND room_id = ? AND revoked_at IS NULL
            """,
            (resolved, device_id, room_id),
        )
        changed = cursor.rowcount
        await cursor.close()
    return bool(changed)


async def revoke_device(
    db: Database, *, room_id: str, device_id: str, now: int | None = None
) -> bool:
    """撤销设备并让其全部会话立即失效。

    两步必须同事务：只改 `devices.revoked_at` 的话，会话查出来仍然有效，
    撤销要等到下一次会话过期才生效——那就不是"立即"了。
    """
    now = int(time.time()) if now is None else now
    async with db.write() as conn:
        cursor = await conn.execute(
            """
            UPDATE devices SET revoked_at = ?
            WHERE id = ? AND room_id = ? AND revoked_at IS NULL
            """,
            (now, device_id, room_id),
        )
        changed = cursor.rowcount
        await cursor.close()
        if not changed:
            return False
        cursor = await conn.execute(
            "UPDATE device_sessions SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
            (now, device_id),
        )
        await cursor.close()
    return True


async def revoke_other_devices(
    db: Database, *, room_id: str, keep_device_id: str, now: int | None = None
) -> int:
    """撤销除当前设备外的全部设备。返回被撤销的数量。

    方案 3.3 把这列为设备管理的五项操作之一，但 9.2 的接口表里没有对应
    条目——本函数由 `DELETE /api/devices` 使用，见附录 F 的说明。
    """
    now = int(time.time()) if now is None else now
    async with db.write() as conn:
        cursor = await conn.execute(
            """
            UPDATE devices SET revoked_at = ?
            WHERE room_id = ? AND id != ? AND revoked_at IS NULL
            """,
            (now, room_id, keep_device_id),
        )
        revoked = cursor.rowcount
        await cursor.close()
        cursor = await conn.execute(
            """
            UPDATE device_sessions SET revoked_at = ?
            WHERE revoked_at IS NULL
              AND device_id IN (SELECT id FROM devices WHERE room_id = ? AND id != ?)
            """,
            (now, room_id, keep_device_id),
        )
        await cursor.close()
    return int(revoked or 0)
