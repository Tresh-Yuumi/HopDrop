"""安全响应头（方案 12）。

**一份定义，两处使用。** 应用中间件给每个响应都带上，`deploy/Caddyfile`
里有一份完全相同的镜像。两份必须一致，`tests/test_security_headers.py`
会逐条比对 Caddyfile 里的内容——靠注释维持一致迟早会失效，靠测试则不会。

---

**为什么应用也要下发一遍。**

本地开发只跑 uvicorn，不跑 Caddy。如果安全头只写在 Caddyfile 里，那么开发
时浏览器不拦任何东西：内联 `<style>` 能跑、`<div onclick=...>` 能跑、从 CDN
取字体也能跑。等到部署到服务器，这些全部被 CSP 拦掉，而那时问题表现为
"页面样式全没了"或"按钮点了没反应"——排查成本远高于在开发时就撞上它。

反过来，只在应用里下发也有问题：应用崩了、Caddy 直接返回 502 时就没有这些
头了。所以两边都留，并且用测试锁住一致性。这不重复，这是**纵深防御 + 单一
事实来源**同时成立的做法。

**CSP 里的两条是硬约束，前端代码必须照着写**（M5 起）：

- `style-src 'self'` → 不能有内联 `<style>`、不能有 `element.style.xxx =`；
- `script-src 'self'` → 不能有内联 `<script>`、不能有 `onclick=` 这类内联
  事件处理器。注意 `<script type="application/json">` 这类"数据岛"同样会被
  拦——CSP 管的是 `<script>` 元素本身，不看 type。

因此前端只能通过 `<link>` 引外部样式、通过 `addEventListener` 绑事件，服务端
要传给前端的数据走 `data-*` 属性而不是内联脚本。
"""

from __future__ import annotations

# CSP 的每一段单独一行，方便测试逐段比对，也方便改的时候看清动了哪一段。
CSP_DIRECTIVES = (
    "default-src 'self'",
    # WebSocket 走同源，`'self'` 已覆盖 ws:// 与 wss://；显式列出 `wss:`
    # 是为了与 Caddyfile 保持逐字一致。两条都能通过。
    "connect-src 'self' wss:",
    "img-src 'self' data: blob:",
    "style-src 'self'",
    "script-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
)

CONTENT_SECURITY_POLICY = "; ".join(CSP_DIRECTIVES)

# 顺序固定，测试按顺序比对。
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
)
