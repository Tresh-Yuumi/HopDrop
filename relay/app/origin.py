"""同源判定。

HTTP 写请求（方案 9.1）与 WebSocket 握手（方案 10）都要校验来源。这两处
必须是同一条规则——只在其中一条通道上成立的防线，等于没有防线。所以抽成
一个函数，`main.py` 与 `api/realtime.py` 都调它。
"""

from __future__ import annotations

from urllib.parse import urlsplit


def same_origin(*, origin: str | None, host: str | None) -> bool:
    """Origin 的 netloc 是否等于 Host 头。

    同源比较而不是"与配置的域名比较"：浏览器发 Origin 时用的就是访问时那个
    主机名，而 Host 头正是同一个值。这样开发（127.0.0.1）、部署（域名）都不
    需要任何配置项，也就没有"配置漏改导致校验失效"的可能。

    两种情况刻意放行 / 拒绝：

    - **没有 Origin 头 → 放行。** 非浏览器客户端（curl、测试、监控）不带这个
      头，而 CSRF 必须借助浏览器才能发生。再加上会话 Cookie 是
      `SameSite=Strict`，跨站请求本来就带不上凭证，这里是第二道防线而非第一道。
    - **`Origin: null` → 拒绝。** 它来自 `file://`、`data:` 或沙箱 iframe，
      没有任何一种属于本应用的正常用法。
    """
    if origin is None:
        return True
    if origin == "null":
        return False
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(host) and parsed.netloc == host
