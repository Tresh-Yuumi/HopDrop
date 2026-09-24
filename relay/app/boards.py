"""文本区（board）的数据访问与生命周期规则。

方案 4.1 / 4.2 / 4.4 与 9.3。本模块只做三件事：可见性判定、可写性判定、
以及把"一次业务写入"翻译成"一次 rev 递增 + 一次区域状态更新"。

---
**两条贯穿全模块的规则，写在这里以免在每个函数里重复解释。**

一、**只有影响快照内容的写入才递增 `rooms.rev`。**
快照（`GET /api/snapshot`）的内容是"可见区域 + 每区最近若干条消息 + 当前
rev + 角色"。所以区域和消息的任何变更都要递增，而设备改名、设备在线时间、
主人链接重置**不递增**——它们不出现在快照里（设备列表是独立接口，前端按需
拉取）。如果把它们也算进去，每台设备每 60 秒刷新一次在线时间就会让所有
客户端白白重拉一次快照。M2 的 `last_seen_at` 注释说的是同一件事。

二、**访客区没有"不可见"状态，只有"不可操作"状态。**
访问非访客区时对访客返回 404（不泄漏存在性）；而对访客区本身调用归档 /
续期 / 改名等操作时返回 409 `guest_board_immutable`。两者的区别是：前者是
"这个资源对你不可见"，后者是"这个资源你看得见，但这个动作对它没有意义"。
"""

from __future__ import annotations

import time
from typing import Any

from .db import Database
from .errors import AppError
from .events import EVENT_BOARD_CREATED, EVENT_BOARD_UPDATED
from .rooms import bump_rev
from .security import new_id

# 保留期档位，与 DDL 的 CHECK 约束一致。
RETENTION_ALLOWED = (0, 30, 60)
RETENTION_PERMANENT = 0
# 方案 4.2：新建时默认 60 天。
RETENTION_DEFAULT = 60

SECONDS_PER_DAY = 86400
# 方案 4.2：到期前 7 天显示提醒。这个天数只影响前端的"即将到期"提示，
# 服务端不做任何基于它的写入，所以放在这里由序列化结果带出去。
EXPIRE_WARN_DAYS = 7

BOARD_NAME_MAX = 64
# 单个房间的区域数量上限。方案没有规定，但"支持新建"不能等于"无限新建"——
# 区域列表要参与每次快照，无上限意味着有人可以靠建区域把快照撑爆。
# 20 这个数来自实际用法：日常、访客区，再加几个按用途分的区，个人工具够用。
BOARD_LIMIT_PER_ROOM = 20

_BOARD_SELECT = """
SELECT b.id,
       b.room_id,
       b.name,
       b.retention,
       b.expires_at,
       b.status,
       b.is_guest,
       b.archived_at,
       b.sort_order,
       b.created_at,
       (SELECT COUNT(*) FROM notes n WHERE n.board_id = b.id AND n.deleted_at IS NULL)
         AS note_count
FROM boards b
"""

# 列表排序键。sort_order 是用户指定的，created_at 只有秒级精度，
# 同一秒建的两个区域会并列——所以必须再拿 id 兜底，否则分页会出现
# 同一条记录跨页重复或整条跳过。三键全用上，顺序才是全序。
_ORDER_KEYS = "b.sort_order ASC, b.created_at ASC, b.id ASC"


def normalize_board_name(raw: object) -> str:
    """校验并规整区域名。不做任何"清洗"——存原样，由渲染层 HTML 转义。"""
    if not isinstance(raw, str):
        raise AppError.bad_request("区域名必须是字符串", "invalid_board_name")
    name = raw.strip()
    if not name:
        raise AppError.bad_request("区域名不能为空", "invalid_board_name")
    if len(name) > BOARD_NAME_MAX:
        raise AppError.bad_request(
            f"区域名不能超过 {BOARD_NAME_MAX} 个字符", "invalid_board_name"
        )
    if any(ch < " " or ch == "\x7f" for ch in name):
        raise AppError.bad_request("区域名不能包含控制字符（含换行和制表符）", "invalid_board_name")
    return name


def normalize_retention(raw: object) -> int:
    """校验保留期档位。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AppError.bad_request("retention 必须是整数", "invalid_retention")
    if raw not in RETENTION_ALLOWED:
        raise AppError.bad_request(
            f"retention 只能是 {'/'.join(str(v) for v in RETENTION_ALLOWED)}（0 表示永久）",
            "invalid_retention",
        )
    return raw


def compute_expires_at(retention: int, now: int) -> int | None:
    """方案 4.2 的到期时间公式。retention=0（永久）时没有到期时间。

    DDL 的 CHECK 约束要求这两者严格对应（永久必须为 NULL，30/60 必须有值），
    所以这个函数是唯一允许产生 expires_at 的地方。
    """
    if retention == RETENTION_PERMANENT:
        return None
    return now + retention * SECONDS_PER_DAY


def serialize_board(row: dict, *, now: int) -> dict:
    """对外形状。字段名统一 camelCase，与 devices / healthz 一致。"""
    expires_at = int(row["expires_at"]) if row["expires_at"] is not None else None
    archived_at = int(row["archived_at"]) if row["archived_at"] is not None else None
    return {
        "id": row["id"],
        "name": row["name"],
        "retention": int(row["retention"]),
        "expiresAt": expires_at,
        "status": row["status"],
        "isGuest": bool(row["is_guest"]),
        "sortOrder": int(row["sort_order"]),
        "archivedAt": archived_at,
        "createdAt": int(row["created_at"]),
        "noteCount": int(row["note_count"]) if row.get("note_count") is not None else 0,
        # 服务端算出"是否已到期"，而不是让前端拿 expiresAt 和本地时钟比：
        # 客户端时钟不准时，界面会显示成"还有 3 天"但写入被拒，那种状态
        # 用户无法自行诊断。判定标准必须只有服务端时间这一个。
        "expired": expires_at is not None and expires_at <= now,
        "expiringSoon": (
            expires_at is not None
            and now < expires_at <= now + EXPIRE_WARN_DAYS * SECONDS_PER_DAY
        ),
    }


async def load_board(
    db: Database,
    *,
    room_id: str,
    board_id: str,
    role: str,
    conn: Any | None = None,
) -> dict:
    """按可见性加载区域。不可见与不存在统一 404。

    `conn` 由写事务传入——写路径必须在事务内先读后写，否则会出现
    "校验通过但提交时状态已变"的窗口。
    """
    row = await db.fetchone(
        _BOARD_SELECT + " WHERE b.id = ?",
        (board_id,),
        conn=conn,
    )
    # 不属于本房间的区域，与不存在的区域返回同一个错误：跨房间猜 id 时
    # 无法区分"猜错了"和"猜对了但没权限"（方案 16.1 的验收项）。
    if row is None or row["room_id"] != room_id:
        raise AppError.not_found("区域不存在")
    if role != "owner" and not row["is_guest"]:
        raise AppError.not_found("区域不存在")
    return row


def assert_board_active(board: dict) -> None:
    """已归档的区域不能再写入（方案 4.2 第 4 步）。"""
    if board["status"] != "active":
        raise AppError.conflict("该区域已归档，不能再写入", "board_archived")


def assert_board_writable(board: dict, *, now: int) -> None:
    """能不能往这个区域追加消息。

    除了归档，还要拦"已到期但尚未归档"这个窗口：到期由每小时的清理任务
    转成归档状态，中间最多有一小时的间隔。如果只判断 status，这段时间里
    用户还能往一个已经过期的区域写东西——而它在几分钟后就会被清理掉。
    """
    assert_board_active(board)
    expires_at = board["expires_at"]
    if expires_at is not None and int(expires_at) <= now:
        raise AppError.conflict("该区域已到期，请先续期", "board_expired")


def assert_board_mutable(board: dict) -> None:
    """访客区不参与新建、重命名、排序、归档和恢复（方案 4.4）。"""
    if board["is_guest"]:
        raise AppError.guest_board_immutable()


async def refresh_guest_expiry(
    db: Database, *, board: dict, now: int, conn: Any
) -> int | None:
    """访客区的滑动续期（方案 4.4）。

    任何一次成功写入都把 `expires_at` 刷成「当前时间 + 30 天」。刻意**不**
    额外递增 rev：本次写入本身已经递增过一次，而 rev 的语义是"快照内容变了"，
    不是"执行了几个 UPDATE"。多递增会让客户端收到一个 rev 缺口（它期待
    +1，实际 +2），从而放弃增量、白拉一次快照。

    必须在写事务内调用，与本次业务写入同时提交——否则会出现"消息写进去了
    但访客区仍按原时间到期清空"的不一致。
    """
    if not board["is_guest"]:
        return None
    new_expiry = now + int(board["retention"]) * SECONDS_PER_DAY
    await conn.execute("UPDATE boards SET expires_at = ? WHERE id = ?", (new_expiry, board["id"]))
    return new_expiry


async def list_boards(
    db: Database,
    *,
    room_id: str,
    role: str,
    status: str = "active",
    limit: int,
    cursor: str | None = None,
    conn: Any | None = None,
) -> tuple[list[dict], str | None]:
    """分页列出可见区域。返回 (行, nextCursor)。

    `status` 参数是为"已归档"导航（方案 11.2）留的：归档区在同一个区域
    资源集合里，只是状态不同。9.3 的接口表没有单列归档列表接口，所以这里
    用一个查询参数表达，而不是另造一个 `/api/archives`。

    `conn` 由调用方传入，用于"已经持锁"的场景（快照要把 rev 与内容读在
    同一次持锁里）。不要为它另开一次 `db.read()`——锁不可重入。
    """
    conditions = ["b.room_id = ?", "b.status = ?"]
    params: list[Any] = [room_id, status]
    if role != "owner":
        conditions.append("b.is_guest = 1")

    if cursor is not None:
        anchor = await db.fetchone(
            _BOARD_SELECT + " WHERE b.id = ?",
            (cursor,),
            conn=conn,
        )
        if anchor is None or anchor["room_id"] != room_id:
            # 游标失效（区域被清理掉）时的正确处理是 400 而不是静默从头开始：
            # 静默重来会让前端的"加载更多"变成无限循环。
            raise AppError.bad_request("分页游标无效", "invalid_cursor")
        conditions.append(
            "(b.sort_order > ? OR (b.sort_order = ? AND (b.created_at > ? "
            "OR (b.created_at = ? AND b.id > ?))))"
        )
        params.extend(
            [
                anchor["sort_order"],
                anchor["sort_order"],
                anchor["created_at"],
                anchor["created_at"],
                anchor["id"],
            ]
        )

    # 多取一条用来判断"还有没有下一页"，比再发一次 COUNT 便宜。
    params.append(limit + 1)
    rows = await db.fetchall(
        _BOARD_SELECT + " WHERE " + " AND ".join(conditions) + f" ORDER BY {_ORDER_KEYS} LIMIT ?",
        tuple(params),
        conn=conn,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1]["id"] if has_more and rows else None
    return rows, next_cursor


async def count_boards(db: Database, *, room_id: str) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS total FROM boards WHERE room_id = ? AND status = 'active'",
        (room_id,),
    )
    return int(row["total"]) if row else 0


async def create_board(
    db: Database,
    *,
    room_id: str,
    name: str,
    retention: int,
    now: int | None = None,
) -> dict:
    """新建区域，并递增 rev。

    `is_guest` 恒为 0：访客区只在建房间时自动生成一次，用户不能手动建，
    否则 `uq_boards_guest`（每房间唯一）会被撞上。
    """
    now = int(time.time()) if now is None else now
    resolved_name = normalize_board_name(name)
    resolved_retention = normalize_retention(retention)

    board_id = new_id()
    async with db.write() as conn:
        # 数量检查放在事务内。放在事务外虽然在本项目里也不会出错（单进程、
        # 写事务串行），但那依赖的是"当前没有并发"这个外部事实；放在这里
        # 依赖的是锁本身，写法与事实一致。
        total = await db.fetchone(
            "SELECT COUNT(*) AS total FROM boards WHERE room_id = ? AND status = 'active'",
            (room_id,),
            conn=conn,
        )
        if int(total["total"]) >= BOARD_LIMIT_PER_ROOM:
            raise AppError.conflict(
                f"区域数量已达上限（{BOARD_LIMIT_PER_ROOM} 个）", "board_limit_reached"
            )

        row = await db.fetchone(
            "SELECT COALESCE(MAX(sort_order), -1) AS max_order FROM boards WHERE room_id = ?",
            (room_id,),
            conn=conn,
        )
        next_order = int(row["max_order"]) + 1
        await conn.execute(
            """
            INSERT INTO boards
              (id, room_id, name, retention, expires_at, status, is_guest, archived_at, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', 0, NULL, ?, ?)
            """,
            (
                board_id,
                room_id,
                resolved_name,
                resolved_retention,
                compute_expires_at(resolved_retention, now),
                next_order,
                now,
            ),
        )
        rev = await bump_rev(conn, room_id)
        # 广播要用的 rev 与要广播的内容必须来自同一个事务，见 notes.create_note
        # 的同款说明。这里在事务内取回整行，顺带把提交后那次查询也省了。
        created = await db.fetchone(_BOARD_SELECT + " WHERE b.id = ?", (board_id,), conn=conn)
        assert created is not None
        db.queue_event(
            room_id=room_id,
            rev=rev,
            event=EVENT_BOARD_CREATED,
            payload={"board": serialize_board(created, now=now)},
        )

    return created


async def update_board(
    db: Database,
    *,
    room_id: str,
    board_id: str,
    name: str | None = None,
    retention: int | None = None,
    sort_order: int | None = None,
    now: int | None = None,
) -> dict:
    """改名、排序、续期。三个动作共用一条 PATCH（方案 9.3）。

    **`retention` 一旦出现就重算 `expires_at`**，无论档位是否变化——这就是
    方案 4.2 的续期（`expires_at = 当前时间 + 原保留天数`）。把"改成另一个
    档位"和"原地续期"合并成同一个操作，是因为它们对数据库的效果完全一样，
    分成两个分支只会多出一处可能写错的地方。

    注意顺序：**先校验可变更性，再校验可写性。** 访客区同时满足"是访客区"
    和"可能已到期"，返回 `guest_board_immutable` 比返回 `board_expired` 更
    准确——对访客区说"请先续期"是错的，它根本不能续期。
    """
    now = int(time.time()) if now is None else now
    resolved_name = normalize_board_name(name) if name is not None else None
    resolved_retention = normalize_retention(retention) if retention is not None else None
    if sort_order is not None and (isinstance(sort_order, bool) or not isinstance(sort_order, int)):
        raise AppError.bad_request("sortOrder 必须是整数", "invalid_sort_order")

    async with db.write() as conn:
        board = await load_board(
            db, room_id=room_id, board_id=board_id, role="owner", conn=conn
        )
        assert_board_mutable(board)
        # 已归档的区域不可改名、不可排序（归档区是只读的，只有导出和恢复
        # 对它有意义）；但**续期在归档前是允许的**——那正是把已到期、尚未
        # 被清理任务归档的区域救回来的唯一手段。
        if board["status"] != "active" and resolved_retention is None:
            raise AppError.conflict("该区域已归档", "board_archived")
        if board["status"] != "active" and (
            resolved_name is not None or sort_order is not None
        ):
            raise AppError.conflict("该区域已归档，只能导出或恢复", "board_archived")

        assignments: list[str] = []
        params: list[Any] = []
        if resolved_name is not None:
            assignments.append("name = ?")
            params.append(resolved_name)
        if resolved_retention is not None:
            assignments.append("retention = ?")
            params.append(resolved_retention)
            assignments.append("expires_at = ?")
            params.append(compute_expires_at(resolved_retention, now))
        if sort_order is not None:
            assignments.append("sort_order = ?")
            params.append(sort_order)

        if not assignments:
            raise AppError.bad_request("没有需要修改的字段", "empty_update")

        params.append(board_id)
        await conn.execute(
            f"UPDATE boards SET {', '.join(assignments)} WHERE id = ?", tuple(params)
        )
        rev = await bump_rev(conn, room_id)
        updated = await db.fetchone(_BOARD_SELECT + " WHERE b.id = ?", (board_id,), conn=conn)
        assert updated is not None
        db.queue_event(
            room_id=room_id,
            rev=rev,
            event=EVENT_BOARD_UPDATED,
            payload={"board": serialize_board(updated, now=now)},
        )

    return updated
