"""数据库层。

方案 2.2 第 2 条：**所有读写都经过同一个 aiosqlite 连接，并用一个全局
asyncio.Lock 串行化。** 本项目写入频率极低（主人写 60 次/分钟上限），
单连接不会有性能问题，但能一次性消灭并发写事务的所有坑。

三条硬约束：
1. 锁不可重入。write()/read() 块内部不得再调用 write()/read()，
   否则同一个 task 会自己等自己，直接死锁。需要"一次持锁读完几样东西"时，
   把连接（conn 参数）往下传，而不是再开一个 read()。
2. 写事务必须显式 BEGIN IMMEDIATE / COMMIT。用 IMMEDIATE 是刻意的：
   一开始就拿写锁，避免"读升级为写"时才发现冲突。
3. 行结果一律用 fetchall()/fetchone() 转成 dict。不用 row_factory，
   因为它是 sqlite3 连接级状态，在 aiosqlite 的封装下行为不够直白。

---

**写事务与广播的绑定关系（M4 加的，理解广播语义的关键）：**

写事务内调用 `queue_event()` 登记"这次提交后要通知谁"，`write()` 在 COMMIT
成功之后、**释放锁之后**统一交给事件汇（`set_event_sink` 注册的回调，实际
就是 WebSocket 广播）。

这样安排解决三件事：

- **回滚不留痕迹**：事务失败时待发事件被丢弃，客户端不会收到一条数据库里
  并不存在的变更；
- **不会出现 rev 空洞的广播**：事件里的 rev 是事务内 `bump_rev` 的返回值，
  提交成功才发出去，客户端收到的一定是连续的那一个；
- **广播失败不污染写入**：发送异常在本层被兜住，`write()` 正常返回。

发事件时**不持锁**：广播要 await 网络发送，持着全局写锁做这件事会把整个
服务卡在一次慢发送上。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence

import aiosqlite

from .events import RoomEvent

logger = logging.getLogger("relay.db")

# 事件汇的签名：收一批已提交的事件，返回时表示"尽力发过了"。
EventSink = Callable[[Sequence[RoomEvent]], Awaitable[None]]


def _rows_to_dicts(cursor: aiosqlite.Cursor, rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    description = cursor.description
    if not description:
        return []
    columns = [item[0] for item in description]
    return [dict(zip(columns, row)) for row in rows]


class Database:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._event_sink: EventSink | None = None
        # 当前写事务待发的事件。`None` 表示"现在不在写事务里"——`queue_event`
        # 靠它把"在事务外登记事件"这种写错法变成一次明确的报错。
        self._pending_events: list[RoomEvent] | None = None

    def set_event_sink(self, sink: EventSink | None) -> None:
        """注册事件汇。装配期调用一次；不注册等于"只写库、不广播"。"""
        self._event_sink = sink

    def queue_event(
        self, *, room_id: str, rev: int, event: str, payload: dict[str, Any]
    ) -> None:
        """登记一条"提交后要广播"的变更。**只能在 `write()` 块内调用。**

        刻意是同步方法：它只往列表里放一个对象，不该让调用方以为这里有
        什么可以 await 的东西——真正的发送发生在 COMMIT 之后。
        """
        if self._pending_events is None:
            raise RuntimeError("queue_event 只能在 db.write() 事务内调用")
        self._pending_events.append(
            RoomEvent(room_id=room_id, rev=rev, event=event, payload=payload)
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库尚未连接")
        return self._conn

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None 关闭 sqlite3 的隐式事务，事务边界完全由本文件控制。
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        # synchronous=FULL：WAL 下每笔提交都 fsync。本服务每分钟最多六十来次写，
        # 这点开销可以忽略，换来的是掉电也不丢已提交的数据——"不丢数据"是红线。
        await conn.execute("PRAGMA synchronous=FULL")
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def ping(self) -> None:
        """最轻的连通性检查。"""
        async with self._lock:
            async with self.conn.execute("SELECT 1") as cursor:
                await cursor.fetchone()

    async def check_writable(self) -> None:
        """用 BEGIN IMMEDIATE 真拿一次写锁——这才叫"数据库可写"。

        healthz 用它，而不是用 SELECT：只读能过但写锁拿不到的情况是存在的
        （例如磁盘满导致 WAL 无法写入）。
        """
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            await self.conn.execute("ROLLBACK")

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            yield self.conn

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        events: list[RoomEvent] = []
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            self._pending_events = []
            try:
                yield self.conn
            except BaseException:
                self._pending_events = None
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")
                events = self._pending_events
                self._pending_events = None

        # 锁已释放，这里再广播。理由见模块说明。
        if events and self._event_sink is not None:
            try:
                await self._event_sink(events)
            except Exception:  # noqa: BLE001 - 广播是尽力而为的，不能反过来影响写入
                logger.exception("广播变更失败 事件数=%d", len(events))

    # ---- 查询辅助 ----

    async def fetchall(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        conn: aiosqlite.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """读一行或读多行都可以直接调用；不持锁时自行加锁。"""
        if conn is not None:
            async with conn.execute(sql, params) as cursor:
                return _rows_to_dicts(cursor, await cursor.fetchall())
        async with self.read() as locked:
            async with locked.execute(sql, params) as cursor:
                return _rows_to_dicts(cursor, await cursor.fetchall())

    async def fetchone(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        conn: aiosqlite.Connection | None = None,
    ) -> dict[str, Any] | None:
        rows = await self.fetchall(sql, params, conn=conn)
        return rows[0] if rows else None
