"""消息（note）的存取、幂等与权限。

方案 4.1 / 4.3 与 9.3。

---
**幂等是这里的核心，也是唯一一处需要小心处理安全的地方。**

`notes.mutation_id` 是**全局唯一列**，不是 `(room_id, mutation_id)` 复合唯一。
这是方案第 8 节 DDL 定下的，不能改。它带来一个不显眼但真实的后果：如果
一段代码在冲突时直接"查出已存在的记录并返回"，那么拿别人房间的 mutation_id
来提交，就会把**别人房间的消息原文读出来**。

所以重放判断必须分两步：先按 mutation_id 查到记录，**再确认它的 room_id
等于当前房间**；不等就返回 409，既不复用也不泄漏。

（客户端用 `crypto.randomUUID()` 产生的 id 不会真的撞上，但"不会撞上"是
客户端行为，不是服务端保证。服务端只能按"输入不可信"来写。）
"""

from __future__ import annotations

import time
from typing import Any

from .boards import assert_board_writable, load_board, refresh_guest_expiry
from .db import Database
from .errors import AppError
from .events import EVENT_NOTE_CREATED, EVENT_NOTE_DELETED, EVENT_NOTE_UPDATED
from .rooms import bump_rev
from .security import new_id

MUTATION_ID_MAX = 64
MUTATION_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

# 列清单与 FROM 子句拆开，是因为快照（`snapshot.py`）要在外面再包一层
# CTE 来取"每个区域最近 N 条"。让它复用这两段，比在那里重抄一遍列名安全：
# 漏抄一个字段的症状是"快照里少了某个字段"，而接口侧完全正常。
NOTE_COLUMNS = """
       n.id,
       n.room_id,
       n.board_id,
       n.author_id,
       n.kind,
       n.content,
       n.pinned,
       n.mutation_id,
       n.deleted_at,
       n.created_at,
       n.updated_at,
       b.name         AS board_name,
       b.status       AS board_status,
       b.is_guest     AS board_is_guest,
       b.retention    AS board_retention,
       b.expires_at   AS board_expires_at,
       d.name         AS author_name
"""

NOTE_FROM = """
FROM notes n
JOIN boards b ON b.id = n.board_id
LEFT JOIN devices d ON d.id = n.author_id
"""

_NOTE_SELECT = f"SELECT {NOTE_COLUMNS} {NOTE_FROM}"


def normalize_mutation_id(raw: object) -> str:
    """校验客户端提供的幂等键。

    限制字符集是防滥用的必要一步：这一列有 UNIQUE 索引，允许任意超长字符串
    就等于允许任何人往索引里塞垃圾。UUID 的形式（含连字符）在允许集内。
    """
    if not isinstance(raw, str):
        raise AppError.bad_request("mutationId 必须是字符串", "invalid_mutation_id")
    if not 1 <= len(raw) <= MUTATION_ID_MAX:
        raise AppError.bad_request(
            f"mutationId 长度必须在 1 到 {MUTATION_ID_MAX} 之间", "invalid_mutation_id"
        )
    if any(ch not in MUTATION_ID_ALLOWED for ch in raw):
        raise AppError.bad_request(
            "mutationId 只能包含字母、数字、连字符和下划线", "invalid_mutation_id"
        )
    return raw


def validate_content(raw: object, *, max_bytes: int) -> str:
    """校验消息正文。

    按 **UTF-8 字节数**而不是字符数限长：库里存的是文本，但传输和存储的
    真实成本是字节。中文一个字符占 3 字节，按字符限长会让实际占用变成
    限制值的三倍。

    不做 trim——正文里的前导空格可能是刻意缩进的代码。但"全是空白"的消息
    没有意义，拒绝。
    """
    if not isinstance(raw, str):
        raise AppError.bad_request("消息内容必须是字符串", "invalid_note_content")
    if not raw.strip():
        raise AppError.bad_request("消息内容不能为空", "invalid_note_content")
    size = len(raw.encode("utf-8"))
    if size > max_bytes:
        raise AppError.too_large(
            f"单条消息不能超过 {max_bytes // 1024} KiB（当前 {size} 字节）",
            "note_too_large",
        )
    return raw


def serialize_note(row: dict) -> dict:
    created_at = int(row["created_at"])
    updated_at = int(row["updated_at"])
    return {
        "id": row["id"],
        "boardId": row["board_id"],
        "authorId": row["author_id"],
        # 作者设备被撤销后记录仍在，名字还能查出来；真到了设备行被清理的
        # 那一天（M8 的孤儿回收），author_id 会由外键置为 NULL，这里给
        # 一个占位名，不让前端拿到 null 去渲染 "null"。
        "authorName": row["author_name"] or "已离开的设备",
        "kind": row["kind"],
        "content": row["content"],
        "pinned": bool(row["pinned"]),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "edited": updated_at > created_at,
        "deletedAt": int(row["deleted_at"]) if row["deleted_at"] is not None else None,
    }


async def load_note(
    db: Database,
    *,
    room_id: str,
    note_id: str,
    role: str,
    conn: Any | None = None,
    allow_deleted: bool = False,
) -> dict:
    """按房间与可见性加载消息。不可见与不存在统一 404。

    `allow_deleted=False`（默认）时，已软删除的消息等同于不存在——M3 的
    编辑与删除接口用这个默认值。回收站（M9）会传 True。
    """
    row = await db.fetchone(_NOTE_SELECT + " WHERE n.id = ?", (note_id,), conn=conn)
    if row is None or row["room_id"] != room_id:
        raise AppError.not_found("消息不存在")
    if role != "owner" and not row["board_is_guest"]:
        raise AppError.not_found("消息不存在")
    if not allow_deleted and row["deleted_at"] is not None:
        raise AppError.not_found("消息不存在")
    return row


def assert_can_modify(row: dict, *, device_id: str, role: str) -> None:
    """谁能改这条消息。

    方案 4.1：主人可编辑任意消息；访客只能编辑自己在访客区发出的消息。
    `board_is_guest` 这一半不是多余的——访客设备在数据上只存在于访客区，
    但把判断写成两个条件，将来若允许访客进入其他区域，这里的语义不会
    悄悄变成"访客可以改自己任何地方的消息"。
    """
    if role == "owner":
        return
    if row["author_id"] == device_id and row["board_is_guest"]:
        return
    raise AppError.forbidden("只能修改自己发出的消息")


async def list_notes(
    db: Database,
    *,
    board_id: str,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:
    """按时间倒序分页。返回 (行, nextCursor)。

    排序键是 `(created_at DESC, id DESC)`。次级键不能省：`created_at` 只有
    秒级精度，一条消息写入的同时另一条也在写入时，两者时间戳相同，只按
    时间排序的话分页边界会出现重复或遗漏。
    """
    conditions = ["n.board_id = ?", "n.deleted_at IS NULL"]
    params: list[Any] = [board_id]

    if cursor is not None:
        anchor = await db.fetchone(
            "SELECT id, created_at FROM notes WHERE id = ?", (cursor,)
        )
        if anchor is None:
            raise AppError.bad_request("分页游标无效", "invalid_cursor")
        conditions.append(
            "(n.created_at < ? OR (n.created_at = ? AND n.id < ?))"
        )
        params.extend([anchor["created_at"], anchor["created_at"], anchor["id"]])

    params.append(limit + 1)
    rows = await db.fetchall(
        _NOTE_SELECT
        + " WHERE "
        + " AND ".join(conditions)
        + " ORDER BY n.created_at DESC, n.id DESC LIMIT ?",
        tuple(params),
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1]["id"] if has_more and rows else None
    return rows, next_cursor


async def create_note(
    db: Database,
    *,
    room_id: str,
    board_id: str,
    device_id: str,
    role: str,
    content: str,
    mutation_id: str,
    now: int | None = None,
) -> tuple[dict, bool]:
    """写入一条文本消息。返回 (行, 是否新建)。

    第二项为 False 表示这是一次幂等重放：**rev 没有被递增**，返回的是
    首次写入的结果。客户端据此把响应时间当成"已送达"，而服务端的状态
    一个字节都没变。

    **区域是在事务内重新加载的。** 接口层当然可以先加载一次用于尽早返回
    404，但那份数据到写入时可能已经过期——锁只在事务期间持有，两次加锁
    之间是一个真实的窗口。这里再读一次，让"判断可写"和"写入"落在同一个
    `BEGIN IMMEDIATE` 里，是唯一能真正排除竞态的写法。

    先查 mutation_id 再插入，同样依赖"事务期间独占写锁"这一点——所以
    不需要靠 UNIQUE 冲突兜底，也就不会走到"先插再捕获异常"那种必须回滚
    整个事务的路径。
    """
    now = int(time.time()) if now is None else now

    async with db.write() as conn:
        existing = await db.fetchone(
            _NOTE_SELECT + " WHERE n.mutation_id = ?", (mutation_id,), conn=conn
        )
        if existing is not None:
            if existing["room_id"] != room_id:
                # 见模块说明：mutation_id 全局唯一，跨越房间就是越权信号。
                raise AppError.conflict("mutationId 已被占用", "mutation_id_conflict")
            return existing, False

        board = await load_board(
            db, room_id=room_id, board_id=board_id, role=role, conn=conn
        )
        assert_board_writable(board, now=now)

        note_id = new_id()
        await conn.execute(
            """
            INSERT INTO notes
              (id, room_id, board_id, author_id, kind, content, pinned, mutation_id,
               deleted_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'text', ?, 0, ?, NULL, ?, ?)
            """,
            (note_id, room_id, board_id, device_id, content, mutation_id, now, now),
        )
        await refresh_guest_expiry(db, board=board, now=now, conn=conn)
        rev = await bump_rev(conn, room_id)
        # 在事务内取回这一行，而不是提交后再查一次：广播要用的 `rev` 与要广播
        # 的内容必须来自同一个事务，否则客户端可能收到一条与 rev 对不上的内容
        # ——它按"rev 正好 +1"增量应用，结果应用错了东西。
        #
        # 取行也必须排在 refresh_guest_expiry 之后：卡片上带的到期时间是本次
        # 续期后的值，而不是续期前的。
        created = await db.fetchone(_NOTE_SELECT + " WHERE n.id = ?", (note_id,), conn=conn)
        assert created is not None
        db.queue_event(
            room_id=room_id,
            rev=rev,
            event=EVENT_NOTE_CREATED,
            payload={"note": serialize_note(created)},
        )

    return created, True


async def update_note(
    db: Database,
    *,
    room_id: str,
    note_id: str,
    device_id: str,
    role: str,
    content: str | None = None,
    pinned: bool | None = None,
    now: int | None = None,
) -> dict:
    """编辑正文或置顶（方案 9.3 的一条 PATCH 承担两件事）。"""
    now = int(time.time()) if now is None else now
    if content is None and pinned is None:
        raise AppError.bad_request("没有需要修改的字段", "empty_update")
    if pinned is not None and role != "owner":
        # 方案 4.1：置顶是主人功能。单独返回 403 而不是笼统的"无权限"，
        # 前端据此可以把置顶按钮直接藏掉。
        raise AppError.forbidden("只有主人可以置顶消息", "owner_only")

    async with db.write() as conn:
        row = await load_note(db, room_id=room_id, note_id=note_id, role=role, conn=conn)
        assert_can_modify(row, device_id=device_id, role=role)
        # 只要求 active 而不要求未到期：到期区域在归档前只是不能再追加，
        # 编辑和删除属于整理动作，不该被到期时间挡住。
        if row["board_status"] != "active":
            raise AppError.conflict("该区域已归档，内容不可修改", "board_archived")

        assignments: list[str] = []
        params: list[Any] = []
        if content is not None:
            assignments.append("content = ?")
            params.append(content)
        if pinned is not None:
            assignments.append("pinned = ?")
            params.append(1 if pinned else 0)
        # updated_at 只在正文变化时推进。置顶不算"编辑"——如果它也推进，
        # 界面上每条被置顶的消息都会挂上"已编辑"标记。
        if content is not None:
            assignments.append("updated_at = ?")
            params.append(now)

        params.append(note_id)
        await conn.execute(
            f"UPDATE notes SET {', '.join(assignments)} WHERE id = ?", tuple(params)
        )
        rev = await bump_rev(conn, room_id)
        updated = await db.fetchone(_NOTE_SELECT + " WHERE n.id = ?", (note_id,), conn=conn)
        assert updated is not None
        db.queue_event(
            room_id=room_id,
            rev=rev,
            event=EVENT_NOTE_UPDATED,
            payload={"note": serialize_note(updated)},
        )

    return updated


async def soft_delete_note(
    db: Database,
    *,
    room_id: str,
    note_id: str,
    device_id: str,
    role: str,
    now: int | None = None,
) -> None:
    """软删除。内容保留，进入回收站，7 天后由清理任务硬删除（方案 4.1）。

    重复删除返回 404 而不是静默成功：`load_note` 默认过滤已删除的行，
    所以第二次调用会走到"消息不存在"。这与"访问不存在的资源返回 404"
    是同一条规则，不需要为删除单独破例。
    """
    now = int(time.time()) if now is None else now
    async with db.write() as conn:
        row = await load_note(db, room_id=room_id, note_id=note_id, role=role, conn=conn)
        assert_can_modify(row, device_id=device_id, role=role)
        if row["board_status"] != "active":
            raise AppError.conflict("该区域已归档，内容不可修改", "board_archived")
        await conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (now, note_id))
        rev = await bump_rev(conn, room_id)
        # 删除的广播只带 id，不带被删的正文——正文已经没有任何客户端该显示了，
        # 再往外发一遍只是徒增一次内容分发面。客户端按 id 把它从列表里摘掉。
        db.queue_event(
            room_id=room_id,
            rev=rev,
            event=EVENT_NOTE_DELETED,
            payload={"noteId": note_id, "boardId": row["board_id"]},
        )
