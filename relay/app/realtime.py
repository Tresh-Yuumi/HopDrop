"""WebSocket 连接表与变更广播（方案 4.3、第 10 节）。

**这一层不承担一致性责任，这是它最重要的性质。** 广播是尽力而为的：发不
出去就算了，不重试、不落库、不补发。事实来源永远是 SQLite，客户端一旦发现
本地 `rev` 与服务端对不上就拉一次 `/api/snapshot`。有了这条兜底，"推送丢包"
就不是一个需要处理的错误，而是一条由客户端收尾的正常路径——这也是本项目
不用持久化事件流的原因（方案 4.3）。

连接表是**进程内内存态**，这正是 uvicorn 不能加 `--workers` 的原因之一
（另一个是全局写锁）：多 worker 会让同一房间的客户端分散在不同的连接表里，
彼此收不到对方的变更，而且症状只是"有时候不刷新"。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Sequence

from fastapi import WebSocket

from .events import RoomEvent
from .state import RuntimeState

logger = logging.getLogger("relay.realtime")

# 空闲超时与重新鉴权的间隔都由 config 提供，这里只放不随部署变化的约定。
#
# 重新鉴权的间隔：方案 3.3 要求"已撤销设备的现有 WebSocket 在下一次心跳或
# 权限检查时关闭"。客户端每 25 秒发一次 ping，所以 60 秒意味着撤销后最迟
# 60 秒断开——同时撤销接口本身会主动踢连接（见 api/devices.py），这里的
# 重鉴权是兜底，覆盖"绕过接口直接改库"和"撤销后客户端仍挂着"两种情况。
REAUTH_INTERVAL_SEC = 60

# 自定义关闭码（4000-4999 段留给应用）。
CLOSE_SESSION_REVOKED = 4401
# 未认证 / 来源不被允许：握手阶段就拒绝，用标准码。
CLOSE_POLICY_VIOLATION = 1008
# 连接数超限：稍后再来。
CLOSE_TRY_AGAIN_LATER = 1013
# 空闲超时。
CLOSE_GOING_AWAY = 1001


@dataclass
class Connection:
    """一条活跃的 WebSocket 连接。

    可变对象（`last_auth_at` 由收发循环推进），但只由持有它的那一个任务改，
    所以不需要加锁。
    """

    id: str
    room_id: str
    device_id: str
    session_id: str
    role: str
    ip: str
    websocket: WebSocket
    connected_at: int
    last_auth_at: int


class ConnectionManager:
    """按房间分组的连接表。

    三个索引（按 id、按房间、按 IP）指向同一批 `Connection` 对象。看起来
    冗余，但这三个维度各自要回答一个不能相互推导的问题：

    - 按 id：广播失败时要能按 id 摘掉一条连接；
    - 按房间：广播时只发给同房间的人（跨房间泄漏消息是这个模块唯一不能
      犯的错）；
    - 按 IP：方案 10 的单 IP 上限。一条连接可能**不属于任何房间**吗？不会
      ——没有有效会话就不会有连接。但它确实会跨越房间维度被计数，所以不能
      用房间索引代替。

    所有方法都是同步的（除了要 await 网络发送的那两个），因此单进程内
    "检查限额 + 登记"这一步天然原子，不需要额外加锁。
    """

    def __init__(self, *, state: RuntimeState, room_limit: int, ip_limit: int) -> None:
        self._state = state
        self._room_limit = room_limit
        self._ip_limit = ip_limit
        self._by_id: dict[str, Connection] = {}
        self._by_room: dict[str, dict[str, Connection]] = {}
        self._by_ip: dict[str, set[str]] = {}

    @property
    def count(self) -> int:
        return len(self._by_id)

    def register(self, conn: Connection) -> str | None:
        """登记一条新连接。成功返回 `None`，否则返回拒绝原因的错误码。

        先判断再登记，中间没有 `await`，所以不存在"两个连接同时抢最后一个
        名额"的窗口。
        """
        if len(self._by_room.get(conn.room_id, {})) >= self._room_limit:
            return "room_connection_limit"
        if len(self._by_ip.get(conn.ip, set())) >= self._ip_limit:
            return "ip_connection_limit"

        self._by_room.setdefault(conn.room_id, {})[conn.id] = conn
        self._by_ip.setdefault(conn.ip, set()).add(conn.id)
        self._by_id[conn.id] = conn
        self._state.websocket_connections = len(self._by_id)
        return None

    def unregister(self, connection_id: str) -> None:
        """摘掉一条连接。**幂等**——收发循环的 finally 与广播失败时都会调用，
        两处谁先谁后不该影响结果。"""
        conn = self._by_id.pop(connection_id, None)
        if conn is None:
            return

        room = self._by_room.get(conn.room_id)
        if room is not None:
            room.pop(connection_id, None)
            if not room:
                del self._by_room[conn.room_id]

        ip_conns = self._by_ip.get(conn.ip)
        if ip_conns is not None:
            ip_conns.discard(connection_id)
            if not ip_conns:
                del self._by_ip[conn.ip]

        self._state.websocket_connections = len(self._by_id)

    async def publish(self, events: Sequence[RoomEvent]) -> None:
        """广播一批已提交的变更。由 `Database.write()` 在 COMMIT 之后调用。

        串行逐条发送即可：单房间上限 20 条连接，且 `send_json` 只是把数据
        放进各自的 asyncio 写队列，不会等待对端确认。为一个不存在的高并发
        场景引入 `asyncio.gather` 只会让"某一条连接卡住"变成难以复现的问题。
        """
        for event in events:
            targets = list(self._by_room.get(event.room_id, {}).values())
            if not targets:
                # 没有人在听是很正常的情况（主人关掉了所有页面）。刻意不记日志：
                # 每次写入都打一条"没人收"只会把真正的异常淹掉。
                continue

            message = {
                "t": "changed",
                "rev": event.rev,
                "event": event.event,
                "payload": event.payload,
            }
            for conn in targets:
                await self._send(conn, message)

    async def _send(self, conn: Connection, message: dict) -> None:
        try:
            await conn.websocket.send_json(message)
        except Exception:  # noqa: BLE001 - 连接坏了不是异常情况，摘掉即可
            logger.debug("推送失败，摘除连接 id=%s", conn.id, exc_info=True)
            self.unregister(conn.id)

    async def close_connections(
        self,
        *,
        room_id: str | None = None,
        device_ids: Sequence[str] | None = None,
        keep_device_id: str | None = None,
        code: int = CLOSE_SESSION_REVOKED,
        message: str = "会话已失效",
    ) -> int:
        """按条件关闭连接，返回关闭数量。

        三个筛选条件是可叠加的：撤销单台设备用 `device_ids`，撤销"其他所有
        设备"用 `room_id` + `keep_device_id`。

        **先发一条 `error` 再关闭**，而不是直接断开：客户端据此能把界面切成
        "已被移除"并停止重连，而不是把它当成一次网络抖动然后无限重试。
        """
        wanted = set(device_ids) if device_ids is not None else None
        targets = [
            conn
            for conn in list(self._by_id.values())
            if (room_id is None or conn.room_id == room_id)
            and (wanted is None or conn.device_id in wanted)
            and (keep_device_id is None or conn.device_id != keep_device_id)
        ]

        for conn in targets:
            try:
                await conn.websocket.send_json(
                    {"t": "error", "code": "session_revoked", "message": message}
                )
                await conn.websocket.close(code=code)
            except Exception:  # noqa: BLE001 - 对端已经断了，那就只剩摘除这一步
                logger.debug("关闭连接失败 id=%s", conn.id, exc_info=True)
            self.unregister(conn.id)
        return len(targets)


def client_ip(*, forwarded_for: str | None, peer_host: str | None) -> str:
    """判定连接来源 IP，用于方案 10 的单 IP 连接上限。

    **前提是 uvicorn 只监听回环地址、且唯一入口是反向代理**（方案 14.3 的
    部署形态，`RELAY_ADDR` 默认就是 `127.0.0.1:8080`）。

    这个前提里有一半很容易被忽略：**反向代理必须是"覆盖式"写这个头。**
    本函数取的是 `X-Forwarded-For` 的**第一个**值，只有当代理用对端地址覆盖
    掉整个头时，它才等于真实来源 IP。Caddy 默认就是覆盖，所以按 Caddy 部署
    时这一点不需要额外说明；而 nginx 的惯用写法
    `$proxy_add_x_forwarded_for` 是**追加**，会把客户端自己带来的伪造值排在
    最前面，任何人就都能伪造来源 IP、绕过限额。因此 `deploy/install.sh`
    生成的站点里写的是 `$remote_addr`，并有一条测试锁住它不被改回追加式。

    如果哪天把 uvicorn 直接暴露到公网，这个头就变成客户端可控的，IP 限额
    随之失效——但失效的只是限额，不是权限：连接仍然要过会话校验。风险不对等，
    所以这里不去引入"可信代理列表"这类需要维护的配置。
    """
    if forwarded_for:
        first = forwarded_for.split(",", 1)[0].strip()
        if first:
            return first
    return peer_host or "unknown"


def now_seconds() -> int:
    return int(time.time())
