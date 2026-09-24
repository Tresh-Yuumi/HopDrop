"""同步协议的两个端点：快照与 WebSocket 推送（方案 4.3、9.3、10）。

**放在同一个模块里是有意的**：它们是同一套机制的两半。`/api/snapshot` 是
事实来源，`/ws` 只是加速器；分开放在两个文件里，读代码的人很容易以为
WebSocket 自己也维护状态。方案的 4.3 就是这么定的——推送尽力而为，丢了
就拉快照，因此"丢推送"不是一个错误，而是一条正常路径。

---

协议上有**一处对方案的补充**，写在这里以免以后有人对着方案 10 的时序图
觉得实现写错了：

> **服务端在 `accept` 之后主动发一次 `hello.ok`，不等客户端的 `hello`。**

方案 10 的时序是"客户端发 `hello` → 服务端回 `hello.ok`"。改成服务端先发，
省掉一个往返，前端也不必处理"连上了但迟迟没有 hello.ok"的超时分支。
客户端的 `hello` 仍然会被响应（内容相同的 `hello.ok`），所以按方案时序实现
的客户端也能正常工作——**代价是 `hello.ok` 可能出现两次，客户端需要幂等
处理**（它本来就该是幂等的：收到 `hello.ok` 只是"记下服务端 rev"）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request, WebSocket

from ..config import Config
from ..db import Database
from ..identity import (
    SESSION_COOKIE,
    AuthContext,
    current_session,
    resolve_session_token,
)
from ..origin import same_origin
from ..realtime import (
    CLOSE_GOING_AWAY,
    CLOSE_POLICY_VIOLATION,
    CLOSE_SESSION_REVOKED,
    CLOSE_TRY_AGAIN_LATER,
    REAUTH_INTERVAL_SEC,
    Connection,
    ConnectionManager,
    client_ip,
)
from ..security import new_id
from ..snapshot import build_snapshot

logger = logging.getLogger("relay.api.realtime")

router = APIRouter()


@router.get("/api/snapshot")
async def get_snapshot(
    request: Request,
    context: Annotated[AuthContext, Depends(current_session)],
) -> dict:
    """全量快照：可见区域、每区最近 N 条消息、当前 rev、角色（方案 9.3）。

    `files` 目前恒为空数组，M7 填充真实元数据。
    """
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db
    return await build_snapshot(db, context=context, cfg=cfg)


def _hello_payload(context: AuthContext, *, now: int) -> dict:
    return {
        "t": "hello.ok",
        "serverTime": now,
        "rev": context.room_rev,
        "role": context.role,
    }


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    """关闭连接，忽略失败。

    对端可能已经自己断了，那时 `close()` 会抛——这不是需要处理的情况，
    因为连接的清理走的是 `finally` 里的 `unregister`。
    """
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001
        logger.debug("关闭 WebSocket 失败", exc_info=True)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    cfg: Config = websocket.app.state.config
    db: Database = websocket.app.state.db
    manager: ConnectionManager = websocket.app.state.realtime

    # 第一步：来源校验。移到会话校验之前是有意的——跨站页面不该有能力
    # 让服务端去查一次数据库。
    if not same_origin(
        origin=websocket.headers.get("origin"),
        host=websocket.headers.get("host"),
    ):
        logger.warning("拒绝跨源 WebSocket origin=%s", websocket.headers.get("origin"))
        await _close_quietly(websocket, CLOSE_POLICY_VIOLATION)
        return

    # 第二步：会话校验。此时还没 accept，所以拒绝是以握手失败（HTTP 403）
    # 的形式发生的，不会先建立一个再立刻断开的连接。
    # 令牌在握手阶段就能从 Cookie 头拿到，不需要先 accept。
    token = websocket.cookies.get(SESSION_COOKIE)
    context = await resolve_session_token(db, token)
    if context is None:
        await _close_quietly(websocket, CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()

    now = int(time.time())
    conn = Connection(
        id=new_id(),
        room_id=context.room_id,
        device_id=context.device_id,
        session_id=context.session_id,
        role=context.role,
        ip=client_ip(
            forwarded_for=websocket.headers.get("x-forwarded-for"),
            peer_host=websocket.client.host if websocket.client else None,
        ),
        websocket=websocket,
        connected_at=now,
        last_auth_at=now,
    )

    # 第三步：抢占额。放在 accept 之后是因为要在被拒时能发一条带原因的消息
    # ——客户端据此能把"连不上"和"连太多了"区分开，前者该退避重试，后者
    # 该先关掉别的页面。
    refused = manager.register(conn)
    if refused is not None:
        await _send_error(websocket, refused, "连接数已达上限，请先关闭其他页面")
        await _close_quietly(websocket, CLOSE_TRY_AGAIN_LATER)
        return

    logger.info(
        "WebSocket 已连接 房间=%s 设备=%s 当前连接数=%d",
        conn.room_id,
        conn.device_id,
        manager.count,
    )

    try:
        await websocket.send_json(_hello_payload(context, now=now))
        await _receive_loop(websocket, db=db, conn=conn, idle_timeout=cfg.ws_idle_timeout_sec)
    finally:
        manager.unregister(conn.id)
        logger.info(
            "WebSocket 已断开 房间=%s 设备=%s 剩余连接数=%d",
            conn.room_id,
            conn.device_id,
            manager.count,
        )


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    try:
        await websocket.send_json({"t": "error", "code": code, "message": message})
    except Exception:  # noqa: BLE001
        logger.debug("发送 error 帧失败 code=%s", code, exc_info=True)


async def _receive_loop(
    websocket: WebSocket,
    *,
    db: Database,
    conn: Connection,
    idle_timeout: int,
) -> None:
    """接收循环。只处理心跳与握手，**不接受任何业务写入**。

    业务写操作一律走 HTTP（方案 10 最后一条）：如果 WebSocket 也能写，就得把
    幂等、限流、Origin、可见性这一整套校验实现两遍，而两份实现迟早会分叉。
    """
    while True:
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=idle_timeout)
        except asyncio.TimeoutError:
            # 方案 10：服务端 60 秒未收到活动则关闭。客户端每 25 秒 ping 一次，
            # 正常情况走不到这里；走到这里说明对端已经没了（进程被杀、网络断了
            # 但 TCP 还没来得及通知）。
            await _close_quietly(websocket, CLOSE_GOING_AWAY)
            return
        except Exception:  # noqa: BLE001 - 连接层异常统一按"断开"处理
            logger.debug("WebSocket 接收异常 id=%s", conn.id, exc_info=True)
            return

        if message.get("type") == "websocket.disconnect":
            return

        if time.time() - conn.last_auth_at >= REAUTH_INTERVAL_SEC:
            if not await _still_valid(websocket, db=db):
                await _send_error(websocket, "session_revoked", "会话已失效")
                await _close_quietly(websocket, CLOSE_SESSION_REVOKED)
                return
            conn.last_auth_at = int(time.time())

        raw = message.get("text")
        if raw is None:
            # 二进制帧。协议里没有它的位置，但也没有理由为此断开连接。
            await _send_error(websocket, "bad_message", "只接受文本帧")
            continue

        await _handle_text(websocket, raw, db=db, conn=conn)


async def _still_valid(websocket: WebSocket, *, db: Database) -> bool:
    """重新校验这条连接背后的会话是否还有效。

    **必须重新读握手时那个令牌，而不是信 `conn` 里的那份拷贝。** 撤销设备改的
    是数据库，`conn` 只是内存里的一个快照——拿它做判断等于什么都没判断。
    """
    token = websocket.cookies.get(SESSION_COOKIE)
    return await resolve_session_token(db, token) is not None


async def _handle_text(websocket: WebSocket, raw: str, *, db: Database, conn: Connection) -> None:
    try:
        data = json.loads(raw)
    except ValueError:
        await _send_error(websocket, "bad_message", "不是合法的 JSON")
        return

    if not isinstance(data, dict):
        await _send_error(websocket, "bad_message", "消息必须是 JSON 对象")
        return

    kind = data.get("t")
    if kind == "ping":
        await websocket.send_json({"t": "pong"})
    elif kind == "hello":
        # 服务端已经主动发过一次 hello.ok，这里再发一次。rev 重新查一次而不是
        # 复用握手时的值：客户端可能过了很久才发 hello，回一个过时的 rev 会让
        # 它误判"我不落后"。查一次库的代价远小于让客户端带着错误的 rev 跑。
        row = await db.fetchone("SELECT rev FROM rooms WHERE id = ?", (conn.room_id,))
        await websocket.send_json(
            {
                "t": "hello.ok",
                "serverTime": int(time.time()),
                "rev": int(row["rev"]) if row is not None else 0,
                "role": conn.role,
            }
        )
    else:
        await _send_error(websocket, "bad_message", f"未知消息类型：{kind!r}")
