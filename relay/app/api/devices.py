"""设备管理。

方案 3.3 列出五项操作，其中四项在这里：改名、撤销单台、撤销全部其他、
重置主人链接。第五项"当前设备主动退出"在 `/api/session/logout`。

**一处需要说明的补充**：3.3 要求"撤销除当前设备外的全部设备"，但 9.2 的
接口表里只有 `DELETE /api/devices/{id}`，没有集合级入口。这里用
`DELETE /api/devices` 表达"撤销其他所有设备"——这是集合资源上 DELETE 的
通行语义，也是唯一不需要在路径里塞一个假 ID 的表达方式。
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..db import Database
from ..devices import list_devices, rename_device, revoke_device, revoke_other_devices
from ..errors import AppError
from ..identity import AuthContext, current_owner, current_session
from ..rooms import rotate_owner_secret

router = APIRouter(prefix="/api")


class RenameRequest(BaseModel):
    # 长度上限在 devices.normalize_device_name 里统一校验，这里只保证它是字符串，
    # 免得出现两处规则不一致。allow_inf_nan 之类的边界交给下游判断。
    name: str = Field(description="新的设备名")


def _serialize(row: dict, *, current_device_id: str) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "createdAt": int(row["created_at"]),
        "lastSeenAt": int(row["last_seen_at"]) if row["last_seen_at"] is not None else None,
        "isCurrent": row["id"] == current_device_id,
    }


@router.get("/devices")
async def get_devices(
    request: Request,
    context: Annotated[AuthContext, Depends(current_owner)],
) -> dict:
    db: Database = request.app.state.db
    rows = await list_devices(db, room_id=context.room_id)
    devices = [_serialize(row, current_device_id=context.device_id) for row in rows]
    return {"devices": devices, "currentDeviceId": context.device_id}


@router.patch("/devices/{device_id}")
async def patch_device(
    device_id: str,
    payload: RenameRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(current_session)],
) -> dict:
    """改名。

    权限判断刻意比方案写得更松一点：主人可以改房间里任何设备的名字
    （设置页的需要），而任意设备可以改**自己**的名字（方案 3.2 第 6 步
    "浏览器可让用户修改默认设备名"——那一步是这台新设备在改自己，
    此时它还未必被当成"主人操作"）。两种情况都不允许改别人的名字。
    """
    if not context.is_owner and device_id != context.device_id:
        raise AppError.forbidden("只能修改自己的设备名")

    db: Database = request.app.state.db
    changed = await rename_device(
        db, room_id=context.room_id, device_id=device_id, name=payload.name
    )
    if not changed:
        # 不属于本房间、已撤销、或根本不存在——统一 404，不泄漏存在性。
        raise AppError.not_found("设备不存在")
    return {"id": device_id}


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(current_owner)],
) -> None:
    """撤销单台设备。

    允许主人撤销自己当前这台（语义等同于登出）。界面上不提供这个入口，
    但接口层不额外禁止——禁止它只会制造一个"看起来该能用却报错"的分支。
    """
    db: Database = request.app.state.db
    revoked = await revoke_device(db, room_id=context.room_id, device_id=device_id)
    if not revoked:
        raise AppError.not_found("设备不存在")


@router.delete("/devices")
async def delete_other_devices(
    request: Request,
    context: Annotated[AuthContext, Depends(current_owner)],
) -> dict:
    """撤销除当前设备外的全部设备。"""
    db: Database = request.app.state.db
    revoked = await revoke_other_devices(
        db, room_id=context.room_id, keep_device_id=context.device_id
    )
    return {"revoked": revoked}


@router.post("/owner-secret/rotate")
async def rotate_secret(
    request: Request,
    context: Annotated[AuthContext, Depends(current_owner)],
) -> dict:
    """重置主人链接。

    返回体里带新 secret 明文——这是它在整个生命周期中唯一一次出现在库外。
    响应走已定型的错误信封与 `Cache-Control: no-store`，且只在已认证的
    主人会话下返回，所以它不会被缓存或跨站读走。
    """
    db: Database = request.app.state.db
    secret = await rotate_owner_secret(db, room_id=context.room_id, now=int(time.time()))
    return {
        "pairPath": f"/pair/{secret}",
        "note": "旧链接已立即失效；已配对设备不受影响。",
    }
