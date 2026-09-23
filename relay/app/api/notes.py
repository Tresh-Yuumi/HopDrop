"""单条消息的编辑与删除。

方案 9.3 的 `PATCH /api/notes/{id}` 和 `DELETE /api/notes/{id}`。

一条实现上的取舍值得写在这里：**这两个接口都用 `current_session` 而不是
`current_owner`。** 因为方案 4.1 明确允许访客编辑自己在访客区发出的消息，
而"是不是自己发的"这个判断必须读库才能做（它取决于消息的作者字段），
放在接口层用依赖表达不出来。所以权限的粗筛在依赖里（必须是已配对设备），
细筛在 `notes.assert_can_modify` 里。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from ..config import Config
from ..db import Database
from ..identity import AuthContext, current_session
from ..notes import serialize_note, soft_delete_note, update_note, validate_content

router = APIRouter(prefix="/api")


class UpdateNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    # StrictBool：把 `pinned: 1` 或 `"true"` 也放进来，只会让"界面上置顶
    # 按钮状态和实际不符"这类问题变得难查。类型错了就报错。
    pinned: StrictBool | None = None


@router.patch("/notes/{note_id}")
async def patch_note(
    note_id: str,
    payload: UpdateNoteRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(current_session)],
) -> dict:
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db

    content = (
        validate_content(payload.content, max_bytes=cfg.note_max_bytes)
        if payload.content is not None
        else None
    )
    row = await update_note(
        db,
        room_id=context.room_id,
        note_id=note_id,
        device_id=context.device_id,
        role=context.role,
        content=content,
        pinned=payload.pinned,
    )
    return {"note": serialize_note(row)}


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(
    note_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(current_session)],
) -> None:
    """软删除，进入回收站。硬删除由清理任务在 7 天后执行（方案 4.1）。"""
    db: Database = request.app.state.db
    await soft_delete_note(
        db,
        room_id=context.room_id,
        note_id=note_id,
        device_id=context.device_id,
        role=context.role,
    )
