"""首页与应用页外壳（方案 11.1、11.2）。

页面这一层从 M5 起只发外壳，所以这里断言的是**外壳的契约**：结构在、
数据以 `data-*` 透出、且**没有任何内联样式或脚本**。最后一条是硬约束——
CSP 的 `style-src 'self'` / `script-src 'self'` 会让内联写法在浏览器里直接
失效，而"失效"的表现是页面样式全没了，不报任何服务端错误。所以必须在
测试里就撞上它。
"""

from __future__ import annotations

import re

from conftest import RelayEnv

from app.identity import SESSION_COOKIE

# 页面里不该出现的写法。每一条都对应一种被 CSP 拦掉的内联形式。
FORBIDDEN_PATTERNS = (
    (re.compile(r"<style[\s>]", re.IGNORECASE), "内联 <style>"),
    (re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE), "内联 <script>（无 src）"),
    (re.compile(r"\son[a-z]+\s*=", re.IGNORECASE), "内联事件处理器（onclick= 等）"),
    (re.compile(r"\sstyle\s*=", re.IGNORECASE), "内联 style 属性"),
)


def _assert_no_inline_html(html: str) -> None:
    for pattern, label in FORBIDDEN_PATTERNS:
        match = pattern.search(html)
        assert not match, f"页面出现{label}：{match.group(0)!r}（CSP 会拦掉它）"


# --------------------------------------------------------------------- 首页


def test_home_is_readable_without_javascript(relay_env: RelayEnv) -> None:
    """未配对首页是纯静态说明页：没有脚本、没有数据请求。"""
    with relay_env.client() as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "<script" not in response.text
    _assert_no_inline_html(response.text)


def test_home_renders_retention_rules_from_config(relay_env: RelayEnv) -> None:
    """数字来自配置，不写死。改配置必须跟着变。"""
    with relay_env.client() as client:
        response = client.get("/")

    assert "保留规则" in response.text
    # 默认值：文件 7 天、归档 90 天
    assert "<strong>7</strong> 天" in response.text
    assert "<strong>90</strong> 天" in response.text
    assert "30 天 / 60 天 / 永久" in response.text


def test_home_shows_file_size_limit_from_config(relay_env: RelayEnv) -> None:
    with relay_env.client() as client:
        response = client.get("/")

    # 默认 20971520 字节 = 20 MiB
    assert "20 MiB" in response.text


def test_home_states_no_end_to_end_encryption(relay_env: RelayEnv) -> None:
    """方案 13 的告知义务：服务端可读、不做审查。这一句不能被删。"""
    with relay_env.client() as client:
        response = client.get("/")

    assert "不提供端到端加密" in response.text


def test_home_omits_compliance_block_when_unconfigured(relay_env: RelayEnv) -> None:
    """未配置备案号与联系方式时，整块不渲染，而不是渲染占位。"""
    with relay_env.client() as client:
        response = client.get("/")

    assert "合规" not in response.text
    assert "beian.miit.gov.cn" not in response.text


def test_home_renders_compliance_block_when_configured(relay_env: RelayEnv) -> None:
    with relay_env.client(
        RELAY_ICP_LICENSE="沪ICP备12345678号-1", RELAY_CONTACT="admin@example.com"
    ) as client:
        response = client.get("/")

    assert "沪ICP备12345678号-1" in response.text
    assert "beian.miit.gov.cn" in response.text
    assert "admin@example.com" in response.text
    # 外链必须带 rel=noopener
    assert 'rel="noopener noreferrer"' in response.text


def test_home_shows_entry_when_paired(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "已配对" in response.text
    assert 'href="/app"' in response.text


# ------------------------------------------------------------------ 应用页


def test_app_page_requires_pairing(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/app")

    assert response.status_code == 401
    # 未配对页同样不引脚本——它没有任何要跑的逻辑
    assert "<script" not in response.text


def test_app_shell_references_all_scripts_in_order(relay_env: RelayEnv) -> None:
    """脚本按依赖顺序加载，且都是 `defer` 的外部脚本。

    顺序即依赖：`app.js` 最后装配，它要能拿到前面几个模块挂在全局上的东西。
    用 `defer` 是因为脚本在 `<body>` 末尾——不写 defer 的话，`<head>` 里
    解析到一半就执行，DOM 还没建好。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")

    assert response.status_code == 200
    found = re.findall(r'<script src="([^"]+)" defer>', response.text)
    assert found == [
        "/static/js/util.js",
        "/static/js/api.js",
        "/static/js/store.js",
        "/static/js/render.js",
        "/static/js/sync.js",
        "/static/js/actions.js",
        "/static/js/app.js",
    ]
    _assert_no_inline_html(response.text)


def test_app_shell_links_stylesheet(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")

    assert '<link rel="stylesheet" href="/static/app.css">' in response.text


def test_app_shell_contains_every_container(relay_env: RelayEnv) -> None:
    """外壳的每个挂载点都在。名字是前后端之间的契约，改名要两边一起改。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")

    for element_id in (
        "hd-app",
        "hd-room-name",
        "hd-conn",
        "hd-boards",
        "hd-notices",
        "hd-boardbar",
        "hd-notes",
        "hd-more",
        "hd-input",
        "hd-send",
        "hd-count",
        "hd-status",
        "hd-read-clip",
        "hd-settings",
        "hd-devices",
        "hd-device-name",
        "hd-toast",
        "hd-scrim",
    ):
        assert f'id="{element_id}"' in response.text, f"外壳缺少 #{element_id}"


def test_read_clipboard_button_starts_hidden(relay_env: RelayEnv) -> None:
    """读取剪贴板是增强项：方法不存在时它必须**不出现**，而不是点了报错
    （方案 5.3 验收项）。所以初始 HTML 里它是 hidden 的，由脚本决定是否显示。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")

    assert 'id="hd-read-clip" hidden' in response.text


def test_app_shell_has_noscript_fallback(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")

    assert "<noscript>" in response.text


def test_app_shell_has_websocket_fallback_notice(relay_env: RelayEnv) -> None:
    """不支持 WebSocket 时页面不该白屏，而要给一条"请手动刷新"。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")

    assert 'id="hd-unsupported"' in response.text
    assert "手动刷新" in response.text


def test_app_shell_flags_insecure_cookie_in_dev(relay_env: RelayEnv) -> None:
    """`RELAY_COOKIE_SECURE=0` 是本地开发配置，页面上必须有明显提示。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")

    assert "Secure 属性已关闭" in response.text


def test_app_shell_has_no_dev_notice_when_cookie_is_secure(relay_env: RelayEnv) -> None:
    """生产配置（`Secure` 开启）下页面不该出现开发提示。

    这里必须**手工带 Cookie 头**而不能靠客户端的 cookie jar：`htpx` 遵守
    Secure 语义，不会把 Secure Cookie 发回 `http://testserver`（见 conftest
    的模块说明）。手工带一个就等于模拟了 HTTPS 这一跳。
    """
    relay_env.add_room()
    with relay_env.client(RELAY_COOKIE_SECURE="1") as client:
        paired = client.get(f"/pair/{relay_env.room.owner_secret}")
        token = paired.cookies[SESSION_COOKIE]
        response = client.get("/app", headers={"Cookie": f"{SESSION_COOKIE}={token}"})

    assert response.status_code == 200
    assert "Secure 属性已关闭" not in response.text


# ------------------------------------------------------------------ 转义


def test_room_and_device_names_are_html_escaped(relay_env: RelayEnv) -> None:
    """区域名与设备名会被写进 `data-*`，必须转义。

    如果不转义，一个叫 `"><script>` 的房间名就能在别人的应用页里注入标签。
    注意这里的房间名是自己起的，所以这不是"攻击者能控制"的场景——但设备名
    来自 User-Agent（可控），两条路径都走同一个 `_esc`，所以一起验。
    """
    relay_env.add_room(name='"><script>alert(1)</script>')
    with relay_env.client() as client:
        client.get(
            f"/pair/{relay_env.room.owner_secret}",
            headers={"User-Agent": '"><img src=x onerror=alert(1)>'},
        )
        response = client.get("/app")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "onerror=" not in response.text
    _assert_no_inline_html(response.text)


def test_robots_meta_blocks_indexing(relay_env: RelayEnv) -> None:
    """个人工具不该被收录。首页也一样——备案要求公开可访问，不等于要出现在
    搜索结果里。"""
    relay_env.add_room()
    with relay_env.client() as client:
        home = client.get("/")
    with relay_env.paired_client() as client:
        app = client.get("/app")

    assert 'content="noindex, nofollow"' in home.text
    assert 'content="noindex, nofollow"' in app.text


def test_layout_declares_a_favicon(relay_env: RelayEnv) -> None:
    """每个页面都要显式声明图标。

    不声明的话浏览器会自己去猜 `/favicon.ico`，而本项目没有 `.ico`——于是
    每个页面的控制台里都多一条红色 404。它本身无害，但**会把真正的错误淹掉**：
    M5 的真机验证里，这条噪音和"首屏快照被守卫吃掉"的静默失败混在同一份输出
    里，一眼看不出哪个才要紧。
    """
    relay_env.add_room()
    with relay_env.client() as client:
        home = client.get("/")
    with relay_env.paired_client() as client:
        app = client.get("/app")

    for response in (home, app):
        assert response.status_code == 200
        assert 'rel="icon"' in response.text
        assert "/static/favicon.svg" in response.text


def test_favicon_file_is_served(relay_env: RelayEnv) -> None:
    """声明了就要真的有这个文件——否则只是把 404 从 `.ico` 挪到了 `.svg`。"""
    with relay_env.client() as client:
        response = client.get("/static/favicon.svg")

    assert response.status_code == 200
    assert "svg" in response.headers.get("content-type", "")
