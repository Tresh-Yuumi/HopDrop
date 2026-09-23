"""数据库层。

方案 2.2 第 2 条：**所有读写都经过同一个 aiosqlite 连接，并用一个全局
asyncio.Lock 串行化。** 本项目写入频率极低（主人写 60 次/分钟上限），
单连接不会有性能问题，但能一次性消灭并发写事务的所有坑。

三条硬约束：
1. 锁不可重入。write()/read() 块内部不得再调用 write()/read()，
   否则同一个 task 会自己等自己，直接死锁。
2. 写事务必须显式 BEGIN IMMEDIATE / COMMIT。用 IMMEDIATE 是刻意的：
   一开始就拿写锁，避免"读升级为写"时才发现冲突。
3. 行结果一律用 fetchall()/fetchone() 转成 dict。不用 row_factory，
   因为它是 sqlite3 连接级状态，在 aiosqlite 的封装下行为不够直白。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite


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
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

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
