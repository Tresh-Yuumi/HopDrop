"""临时页面。

---

**这两个页面是脚手架，M5 会用真正的应用页替换掉。**

它们存在的唯一原因是让 M2 可独立验证：配对的验收标准是"两台设备配对后
长期登录、撤销立即生效"，用 curl 只能验证到 Cookie 和 401，验证不了
"浏览器里真的还在登录状态"。所以这里放一个服务端渲染的极小页面。

三条自我约束，避免脚手架长成正式代码：

1. **不使用任何内联 `<style>` 或 `<script>`。** 部署后 Caddy 会下发
   `style-src 'self'`、`script-src 'self'`，内联资源会被拦掉；
2. **不使用任何模板引擎**，就是字符串拼接 + 显式转义（转义是方案 12 的
   安全主防线，不引入系统反而更容易检查）；
3. 设备列表服务端渲染，不依赖 JS——HTML 表单发不出 DELETE，所以撤销操作
   这一步留给 M5 的前端，本页只负责"看得见登录状态"。
"""

from __future__ import annotations

import html
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import Config
from ..db import Database
from ..devices import list_devices
from ..identity import resolve_session

router = APIRouter()

_OWNER_LINK_HINT = "请使用主人链接（形如 /pair/xxxx）完成首次配对。"


def _layout(title: str, body: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head><body>"
        f"{body}"
        "</body></html>"
    )


def _format_ts(value: object) -> str:
    if value is None:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(value)))


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """M5 会替换为方案 11.1 的正式首页（含用途说明、访客码入口、备案号）。"""
    context = await resolve_session(request)

    if context is not None:
        body = (
            "<h1>HopDrop</h1>"
            f"<p>当前设备已配对：{html.escape(context.device_name)}"
            f"（{'主人' if context.is_owner else '访客'}）</p>"
            '<p><a href="/app">进入应用页</a></p>'
        )
    else:
        body = f"<h1>HopDrop</h1><p>{html.escape(_OWNER_LINK_HINT)}</p>"

    body += "<hr><p>临时页面，正式界面见里程碑 M5。</p>"
    return HTMLResponse(_layout("HopDrop", body))


@router.get("/app", response_class=HTMLResponse)
async def app_page(request: Request) -> HTMLResponse:
    """M5 会替换为方案 11.2 的正式应用页。"""
    context = await resolve_session(request)

    if context is None:
        body = (
            "<h1>尚未配对</h1>"
            f"<p>{html.escape(_OWNER_LINK_HINT)}</p>"
            '<p><a href="/">返回首页</a></p>'
        )
        return HTMLResponse(_layout("HopDrop · 未配对", body), status_code=401)

    cfg: Config = request.app.state.config
    db: Database = request.app.state.db
    rows = await list_devices(db, room_id=context.room_id)

    items = []
    for row in rows:
        marker = " ← 当前设备" if row["id"] == context.device_id else ""
        items.append(
            "<li>"
            f"{html.escape(row['name'])}"
            f"（{html.escape(row['role'])}）"
            f"｜配对于 {_format_ts(row['created_at'])}"
            f"｜最近在线 {_format_ts(row['last_seen_at'])}"
            f"{marker}"
            "</li>"
        )

    body = (
        f"<h1>{html.escape(context.room_name)}</h1>"
        f"<p>设备：{html.escape(context.device_name)}"
        f"（{'主人' if context.is_owner else '访客'}）</p>"
        f"<p>房间修订号 rev={context.room_rev}</p>"
        "<h2>设备列表</h2>"
        f"<ul>{''.join(items)}</ul>"
        '<form method="post" action="/api/session/logout">'
        "<button type=\"submit\">退出当前设备</button>"
        "</form>"
        "<hr>"
        "<p>临时页面，正式界面见里程碑 M5。"
        "撤销设备需要 DELETE 请求，命令行示例见 README。</p>"
    )
    if not cfg.cookie_secure:
        body += "<p><strong>警告：Cookie 的 Secure 属性已关闭，此状态仅可用于本地开发。</strong></p>"

    return HTMLResponse(_layout("HopDrop · 应用页", body))
