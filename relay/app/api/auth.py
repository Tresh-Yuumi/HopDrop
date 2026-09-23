"""配对入口与会话退出。

方案 3.2 的配对流程必须全自动，不增加任何输入步骤。这里的实现严格按
六步走：校验哈希 → 建设备和会话 → Set-Cookie → 303 跳转 /app。

第 5 步的 303 不是为了好看：secret 出现在 URL 里，如果不跳转，它会留在
地址栏、进入浏览历史，并且被后续任何一次相对路径请求的 Referer 带出去。
把它从地址栏赶走是安全要求，不是体验偏好。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from ..config import Config
from ..db import Database
from ..devices import create_owner_device
from ..errors import AppError
from ..identity import (
    SESSION_COOKIE,
    clear_session_cookie,
    resolve_session,
    set_session_cookie,
)
from ..rooms import find_room_by_owner_secret
from ..security import hash_token

router = APIRouter()

# secret 是 43 字符的 url-safe base64。给一个宽松但有限的上界，
# 避免有人拿超长路径来消耗哈希计算。超出范围一律 404，不区分原因。
_SECRET_MAX_LEN = 128

_APP_PATH = "/app"
_HOME_PATH = "/"


@router.get("/pair/{secret}")
async def pair(request: Request, secret: str):
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db

    if not 1 <= len(secret) <= _SECRET_MAX_LEN:
        raise AppError.not_found("链接无效或已失效")

    room = await find_room_by_owner_secret(db, secret)
    if room is None:
        raise AppError.not_found("链接无效或已失效")

    existing = await resolve_session(request)
    if existing is not None and existing.room_id == room["id"] and existing.is_owner:
        # 已配对的浏览器再次打开主人链接：直接放行，不再造一台新设备。
        # 主人链接是可以长期收藏的，不这样做的话每点一次书签就多一台设备。
        response = RedirectResponse(_APP_PATH, status_code=303)
    else:
        pairing = await create_owner_device(
            db,
            room_id=room["id"],
            ua=request.headers.get("user-agent"),
        )
        response = RedirectResponse(_APP_PATH, status_code=303)
        set_session_cookie(response, pairing.session_token, secure=cfg.cookie_secure)

    # 方案 12：配对响应设置 Referrer-Policy: no-referrer。
    # 中间件已全局设置，这里再显式写一次，保证即使将来调整中间件也不会丢。
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/api/session/logout")
async def logout(request: Request) -> Response:
    """当前设备退出。

    刻意**不要求会话有效**：令牌可能已经过期或被撤销，此时"退出"应当仍然
    成功（清掉本地 Cookie），而不是返回 401 让前端卡在一个删不掉的登录态里。
    所以这个接口是幂等的。
    """
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        now = int(time.time())
        async with db.write() as conn:
            cursor = await conn.execute(
                "UPDATE device_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (now, hash_token(token)),
            )
            await cursor.close()

    # 表单提交（浏览器）看得到跳转，fetch 拿 204。这不是权宜之计：
    # 两类客户端对"成功"的期望本来就不一样。
    if "text/html" in request.headers.get("accept", ""):
        response: Response = RedirectResponse(_HOME_PATH, status_code=303)
    else:
        response = Response(status_code=204)

    clear_session_cookie(response, secure=cfg.cookie_secure)
    return response
