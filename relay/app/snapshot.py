"""全量快照（方案 4.3、9.3）。

客户端的一致性完全落在这个接口上：任何一次"本地 `rev` 与服务端对不上"都以
拉一次快照收场。所以它的目标不是省流量，而是**一次调用就能把界面重建到与
服务端一致的状态**——包括那些从未收到过推送的客户端（首次加载、长时间离线）。

---

**两条实现上的硬要求。**

一、**`rev` 与内容必须在同一次持锁里读完。**

如果先查 rev、再查区域列表，两次之间可能插进一次写入：客户端拿到的是
"rev = N，但内容是 N+1 的"。它会把这份内容记成 rev N，下一次收到
`changed(rev = N+1)` 时判定"正好接上"，于是把已经存在的变更再应用一遍
——同一条消息在界面上出现两次。

`db.read()` 持锁期间不允许再调用 `db.read()`（锁不可重入），所以下面的
每个查询都把连接（`conn`）往下传。

二、**`rev` 取自数据库，不取自会话。**

会话里的 `room_rev` 是请求开始时读的，比内容旧。快照要返回的是"这份内容
对应的 rev"，那就是库里此刻的值。
"""

from __future__ import annotations

import time

from .boards import BOARD_LIMIT_PER_ROOM, list_boards, serialize_board
from .config import Config
from .db import Database
from .identity import AuthContext
from .notes import NOTE_COLUMNS, NOTE_FROM, NOTE_ORDER_DESC, serialize_note


async def load_recent_notes(
    db: Database,
    *,
    board_ids: list[str],
    limit: int,
    conn=None,
) -> dict[str, list[dict]]:
    """一次取回多个区域各自最近的 `limit` 条消息，返回按区域分组的字典。

    用窗口函数而不是"每个区域查一次"：20 个区域就是 20 次往返，且每次都
    要走一遍 JOIN。这里先用 CTE 把每个区域的前 N 条 id 选出来（走
    `idx_notes_board_time`），再 JOIN 回主表拿展示需要的字段——外层只处理
    最多 `区域数 × limit` 行，不会因为某个区域攒了几万条历史而变慢。

    排序与分页接口一致：**时间倒序**（最新的在前）。前端重建时如果按正序
    渲染，反转一次即可；两端用同一个顺序，比各自记一套更容易对齐。
    """
    if not board_ids:
        return {}

    placeholders = ", ".join("?" for _ in board_ids)
    sql = f"""
    WITH keep AS (
        SELECT id
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                     PARTITION BY board_id ORDER BY created_at DESC, rowid DESC
                   ) AS rn
            FROM notes
            WHERE board_id IN ({placeholders}) AND deleted_at IS NULL
        )
        WHERE rn <= ?
    )
    SELECT {NOTE_COLUMNS}
    {NOTE_FROM}
    JOIN keep ON keep.id = n.id
    ORDER BY n.board_id ASC, {NOTE_ORDER_DESC}
    """
    rows = await db.fetchall(sql, (*board_ids, limit), conn=conn)

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["board_id"], []).append(row)
    return grouped


async def build_snapshot(
    db: Database,
    *,
    context: AuthContext,
    cfg: Config,
    now: int | None = None,
) -> dict:
    """组装快照。

    可见性规则与 `/api/boards` 完全一致（访客只拿到访客区），因为两者由同一个
    `list_boards` 产出——分成两套判断迟早会出现"列表里看不见、快照里有"。
    """
    now = int(time.time()) if now is None else now

    async with db.read() as conn:
        rev_row = await db.fetchone(
            "SELECT rev FROM rooms WHERE id = ?", (context.room_id,), conn=conn
        )
        rev = int(rev_row["rev"]) if rev_row is not None else context.room_rev

        # 区域数量上限就是 20（boards.BOARD_LIMIT_PER_ROOM），所以这个 limit
        # 不是"取一批看看"，而是"全部"。用别的值会让快照凭空少掉几个区域。
        board_rows, _ = await list_boards(
            db,
            room_id=context.room_id,
            role=context.role,
            status="active",
            limit=BOARD_LIMIT_PER_ROOM,
            conn=conn,
        )
        notes_by_board = await load_recent_notes(
            db,
            board_ids=[row["id"] for row in board_rows],
            limit=cfg.snapshot_note_limit,
            conn=conn,
        )

    boards = []
    for row in board_rows:
        payload = serialize_board(row, now=now)
        payload["notes"] = [
            serialize_note(note) for note in notes_by_board.get(row["id"], [])
        ]
        boards.append(payload)

    return {
        "rev": rev,
        "role": context.role,
        "serverTime": now,
        "room": {"id": context.room_id, "name": context.room_name},
        "device": {"id": context.device_id, "name": context.device_name},
        "boards": boards,
        # 文件元数据由 M7 填充。现在给一个空列表而不是省掉这个键：前端在
        # M5 就能按最终形状写渲染逻辑，M7 接进来时不需要改前端。
        "files": [],
    }
