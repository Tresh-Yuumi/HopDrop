"""安全响应头的一致性与覆盖面（方案 12）。

`app/security_headers.py` 说"Caddyfile 里有一份完全相同的镜像，测试逐条比对"。
这个文件就是那句话的兑现。

**为什么值得为"两份一样的常量"写测试。** 一致性的维护成本不在写下来那一刻，
而在之后每一次有人只改一边时。注释做不到约束，diff 也看不出来（一个文件是
Python 常量、另一个是 Caddy 配置）。一条测试把"漂移"从"部署后才发现"变成
"提交前就红"。
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import RelayEnv

from app.security_headers import CONTENT_SECURITY_POLICY, SECURITY_HEADERS

CADDYFILE = Path(__file__).resolve().parent.parent.parent / "deploy" / "Caddyfile"

# 应用下发、Caddy 不下发的两项在 Caddyfile 里以 `-Xxx` 形式出现。
CADDY_HEADER_LINE = re.compile(r'^\s*(?P<name>[A-Za-z][A-Za-z0-9-]*)\s+"(?P<value>.*)"\s*$')


def _caddy_headers() -> dict[str, str]:
    """从 Caddyfile 的 `header { ... }` 块里抽出配置的头。

    只做够用的解析：找 `header {` 到配对的 `}`，逐行取 `名字 "值"`。不引
    Caddy 的解析器——那需要跑 Caddy 本身，而这里要验证的只是"值一样"。
    """
    text = CADDYFILE.read_text(encoding="utf-8")
    start = text.index("header {")
    depth = 0
    lines: list[str] = []
    for line in text[start:].splitlines():
        if line.strip().endswith("{"):
            depth += 1
            if depth == 1:
                continue
        if line.strip() == "}":
            depth -= 1
            if depth == 0:
                break
        if depth >= 1:
            lines.append(line)

    headers: dict[str, str] = {}
    for line in lines:
        match = CADDY_HEADER_LINE.match(line)
        if match:
            headers[match.group("name").lower()] = match.group("value")
    return headers


def test_caddyfile_exists() -> None:
    assert CADDYFILE.is_file(), f"找不到 {CADDYFILE}"


def test_caddyfile_mirrors_application_headers() -> None:
    """Caddyfile 里必须逐条出现应用下发的每个头，且值完全相同。"""
    caddy = _caddy_headers()

    for name, value in SECURITY_HEADERS:
        key = name.lower()
        assert key in caddy, f"Caddyfile 缺少 {name}"
        assert caddy[key] == value, f"{name} 的值与应用不一致"


def test_caddyfile_hides_server_banner() -> None:
    """Caddy 侧用 `-Server` 抹掉版本号。这是 Caddy 独有的一项——应用并发不出
    `Server` 头（uvicorn 自己写），所以只能在这里配。"""
    text = CADDYFILE.read_text(encoding="utf-8")
    assert re.search(r"^\s*-Server\s*$", text, re.MULTILINE)


def test_application_sends_every_header(relay_env: RelayEnv) -> None:
    """任取三条不同类型的路径，逐个响应都带上全部头。"""
    with relay_env.client() as client:
        responses = [client.get("/"), client.get("/api/healthz"), client.get("/static/app.css")]

    for response in responses:
        for name, value in SECURITY_HEADERS:
            assert response.headers.get(name.lower()) == value


def test_csp_forbids_inline_style_and_script(relay_env: RelayEnv) -> None:
    """前端的写法受这两条约束，所以它们必须是"不许内联"而不是"允许自源"。

    如果哪天有人把 `'unsafe-inline'` 加进来图省事，前端所有内联写法都会开始
    工作——然后在生产环境继续工作，而方案 12 的意图就失效了。
    """
    assert "style-src 'self'" in CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    # 数据岛（<script type="application/json">）同样被拦，所以这里不许放开
    assert "object-src 'none'" in CONTENT_SECURITY_POLICY
    assert "base-uri 'none'" in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY


def test_connect_src_allows_websocket() -> None:
    """WebSocket 走同源，`'self'` 已覆盖，但 `wss:` 也要在——两处都写上时
    内核按并集处理，不会因为多写一条而拒绝。"""
    assert "connect-src 'self' wss:" in CONTENT_SECURITY_POLICY
