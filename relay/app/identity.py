"""会话解析与权限依赖。

每个请求都重新从数据库计算角色。方案 12 明确要求：**不信任客户端传入的
`roomId` 或 `role`**，所以它们只存在于本模块返回的 `AuthContext` 里，
由服务端从会话反查得出。

撤销的生效方式是"查不到有效会话"，不是"删掉已发出的令牌"——这样
HTTP 请求在下一次到达时立刻 401，不需要维护任何黑名单。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from .db import Database
from .errors import AppError
from .security import hash_token

logger = logging.getLogger("relay.identity")

SESSION_COOKIE = "relay_session"

# Cookie 的寿命。方案 3.2 要求"刷新、关闭浏览器、重启设备后仍保持登录"，
# 所以必须是持久 Cookie 而不是会话 Cookie。
# 400 天是 Chromium 对 Max-Age 的上限，写更长也会被截断。
SESSION_COOKIE_MAX_AGE = 400 * 24 * 3600

# last_seen_at 的写入节流。它是展示字段，不参与鉴权，没必要每个请求都写。
LAST_SEEN_THROTTLE_SEC = 60

_SESSION_SQL = """
SELECT s.id                AS session_id,
       s.revoked_at        AS session_revoked_at,
       s.expires_at        AS session_expires_at,
       d.id                AS device_id,
       d.name              AS device_name,
       d.role              AS role,
       d.revoked_at        AS device_revoked_at,
       d.last_seen_at      AS last_seen_at,
       r.id                AS room_id,
       r.name              AS room_name,
       r.rev               AS room_rev
FROM device_sessions s
JOIN devices d ON d.id = s.device_id
JOIN rooms   r ON r.id = d.room_id
WHERE s.token_hash = ?
"""


@dataclass(frozen=True)
class AuthContext:
    room_id: str
    room_name: str
    room_rev: int
    device_id: str
    device_name: str
    role: str
    session_id: str

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def _is_usable(row: dict, now: int) -> bool:
    if row["session_revoked_at"] is not None:
        return False
    if row["device_revoked_at"] is not None:
        return False
    expires_at = row["session_expires_at"]
    return expires_at is None or int(expires_at) > now


async def _touch_last_seen(db: Database, row: dict, now: int) -> None:
    """节流地刷新 last_seen_at。

    刻意**不递增 `rooms.rev`**：设备在线时间只出现在设置页，不在消息流里。
    若把它算作"影响客户端界面的写入"，每台设备每 60 秒就会让所有客户端
    重拉一次快照——把一个展示字段变成了持续的同步风暴。

    写入失败只记日志，不能让鉴权失败。
    """
    previous = row["last_seen_at"]
    if previous is not None and now - int(previous) < LAST_SEEN_THROTTLE_SEC:
        return
    try:
        async with db.write() as conn:
            await conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (now, row["device_id"]),
            )
    except Exception:  # noqa: BLE001 - 展示字段，写失败不影响本次请求
        logger.warning("刷新 last_seen_at 失败 device_id=%s", row["device_id"], exc_info=True)


async def resolve_session(request: Request) -> AuthContext | None:
    """从 Cookie 解析会话。无效或已撤销时返回 None，不抛异常。

    返回 None 的那种调用方（首页、配对入口）需要自行决定如何响应；
    需要强制登录的接口用 `current_session` / `current_owner`。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    db: Database = request.app.state.db
    row = await db.fetchone(_SESSION_SQL, (hash_token(token),))
    if row is None:
        return None

    now = int(time.time())
    if not _is_usable(row, now):
        return None

    await _touch_last_seen(db, row, now)

    return AuthContext(
        room_id=row["room_id"],
        room_name=row["room_name"],
        room_rev=int(row["room_rev"]),
        device_id=row["device_id"],
        device_name=row["device_name"],
        role=row["role"],
        session_id=row["session_id"],
    )


def set_session_cookie(response, token: str, *, secure: bool) -> None:
    """写入会话 Cookie。

    `max_age` 必须给：不给就是会话 Cookie，关掉浏览器就没了，直接违反
    "关闭浏览器、重启设备后仍保持登录"这条验收标准。

    `samesite="strict"` 是 CSRF 的第一道防线——跨站请求根本不会带上它，
    所以服务端即使漏了某一处 Origin 校验，写操作也拿不到凭证。
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response, *, secure: bool) -> None:
    """删除会话 Cookie。属性必须与写入时一致，否则浏览器删不掉。"""
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


async def current_session(request: Request) -> AuthContext:
    """任何已配对设备（主人或访客）。"""
    context = await resolve_session(request)
    if context is None:
        raise AppError.unauthorized()
    return context


async def current_owner(
    context: Annotated[AuthContext, Depends(current_session)],
) -> AuthContext:
    """仅主人。访客调用时返回 403，不是 404——资源本身对访客是可见的，
    只是没有这个操作权限。"""
    if not context.is_owner:
        raise AppError.forbidden("该操作仅主人可用")
    return context
