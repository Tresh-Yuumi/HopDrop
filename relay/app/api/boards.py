"""区域与消息的读取、创建接口。

方案 9.3 的接口表里，区域和"区域下的消息集合"是同一条资源路径，所以它们
在同一个 router 里；而单条消息（`/api/notes/{id}`）是另一条路径，放在
`api/notes.py`。

权限分工与 M2 一致：**接口层只决定"这个动作谁能发起"，业务层决定"这个
动作对当前状态是否成立"。** 所以这里用 `current_session` 还是
`current_owner` 是权限问题的全部答案，而"区域已归档"、"访客区不可变更"
这类判断留在 `boards.py` / `notes.py` 里——它们需要读库才能回答。
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from ..boards import (
    RETENTION_DEFAULT,
    list_boards,
    load_board,
    serialize_board,
    update_board,
)
from ..boards import create_board as create_board_row
from ..config import Config
from ..db import Database
from ..errors import AppError
from ..identity import AuthContext, current_owner, current_session
from ..notes import (
    create_note,
    list_notes,
    normalize_mutation_id,
    serialize_note,
    validate_content,
)

router = APIRouter(prefix="/api")


class CreateBoardRequest(BaseModel):
    # extra="forbid"：字段名拼错时返回 400，而不是静默忽略。
    # 静默忽略在这个接口上尤其危险——客户端以为设了 30 天保留期，
    # 实际拿到的是默认 60 天，而它不会收到任何提示。
    #
    # StrictInt 而不是 int：pydantic 的宽松模式会把字符串 `"30"` 转成数字，
    # 于是 `{"retention": "30"}` 被"善意地"接受了。这看起来友好，实际是在
    # 掩盖客户端 bug——前端用 JSON.stringify 发出的数字永远是数字类型，
    # 出现字符串就说明调用方写错了，应该当场报错。
    model_config = ConfigDict(extra="forbid")

    name: str
    retention: StrictInt = RETENTION_DEFAULT


class UpdateBoardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    retention: StrictInt | None = None
    sortOrder: StrictInt | None = None


class CreateNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    mutationId: str


def _resolve_limit(raw: int | None, cfg: Config) -> int:
    """分页大小的解析。超上限时明确拒绝而不是静默截断。

    静默截断会让客户端的"下一页"逻辑永远算错——它以为自己拿到了 500 条，
    实际只有 200 条，而它没有任何办法察觉。
    """
    if raw is None:
        return cfg.page_size_default
    if raw > cfg.page_size_max:
        raise AppError.bad_request(
            f"limit 不能超过 {cfg.page_size_max}", "invalid_limit"
        )
    return raw


@router.get("/boards")
async def get_boards(
    request: Request,
    context: Annotated[AuthContext, Depends(current_session)],
    status: Annotated[str, Query(pattern="^(active|archived)$")] = "active",
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
) -> dict:
    """列出可见区域。访客只会拿到访客区（方案 16.1）。"""
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db
    now = int(time.time())

    rows, next_cursor = await list_boards(
        db,
        room_id=context.room_id,
        role=context.role,
        status=status,
        limit=_resolve_limit(limit, cfg),
        cursor=cursor,
    )
    return {
        "boards": [serialize_board(row, now=now) for row in rows],
        "nextCursor": next_cursor,
        "rev": context.room_rev,
    }


@router.post("/boards", status_code=201)
async def post_board(
    payload: CreateBoardRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(current_owner)],
) -> dict:
    """新建区域。访客无此权限——他只有访客区（方案 4.4）。"""
    db: Database = request.app.state.db
    now = int(time.time())
    row = await create_board_row(
        db,
        room_id=context.room_id,
        name=payload.name,
        retention=payload.retention,
        now=now,
    )
    return {"board": serialize_board(row, now=now)}


@router.patch("/boards/{board_id}")
async def patch_board(
    board_id: str,
    payload: UpdateBoardRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(current_owner)],
) -> dict:
    """改名、排序、续期。三件事共用一条 PATCH，字段全部可选。"""
    db: Database = request.app.state.db
    now = int(time.time())
    row = await update_board(
        db,
        room_id=context.room_id,
        board_id=board_id,
        name=payload.name,
        retention=payload.retention,
        sort_order=payload.sortOrder,
        now=now,
    )
    return {"board": serialize_board(row, now=now)}


@router.get("/boards/{board_id}/notes")
async def get_board_notes(
    board_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(current_session)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
) -> dict:
    """分页读取历史消息。按时间倒序，游标是上一页最后一条的 id。"""
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db
    now = int(time.time())

    # 先确认区域可见。不复用这个查询的其它字段——可写性与到期状态一律
    # 以写接口事务内的判断为准，这里只为了避免"给不可见的区域返回消息"。
    board = await load_board(
        db, room_id=context.room_id, board_id=board_id, role=context.role
    )
    rows, next_cursor = await list_notes(
        db, board_id=board_id, limit=_resolve_limit(limit, cfg), cursor=cursor
    )
    return {
        "board": serialize_board(board, now=now),
        "notes": [serialize_note(row) for row in rows],
        "nextCursor": next_cursor,
    }


@router.post("/boards/{board_id}/notes", status_code=201)
async def post_board_note(
    board_id: str,
    payload: CreateNoteRequest,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(current_session)],
) -> dict:
    """新增文本消息。

    状态码有区别地表达了两件事：首次写入 `201`，幂等重放 `200`。
    方案 9.1 只说了"成功创建返回 201，幂等重放返回原结果"，没说重放用
    哪个码；用 200 是因为它让客户端能区分"这条是我发出去的"和"这条我
    之前就发过"，而两者的界面反馈不该一样（后者不该再弹一次"已发送"）。
    """
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db

    content = validate_content(payload.content, max_bytes=cfg.note_max_bytes)
    mutation_id = normalize_mutation_id(payload.mutationId)

    row, created = await create_note(
        db,
        room_id=context.room_id,
        board_id=board_id,
        device_id=context.device_id,
        role=context.role,
        content=content,
        mutation_id=mutation_id,
    )
    if not created:
        response.status_code = 200
    return {"note": serialize_note(row)}
