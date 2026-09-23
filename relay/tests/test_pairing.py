"""配对、会话与来源校验。

对应方案 16.1：
- 主人链接首次打开后地址栏不再包含 secret；
- 刷新、关闭浏览器、重启设备后主人仍保持登录；
- 撤销设备后 HTTP 失效；
- 重置主人链接后旧链接失效、已配对设备仍可用。
"""

from __future__ import annotations

import time

from app.identity import SESSION_COOKIE, SESSION_COOKIE_MAX_AGE
from app.security import hash_token


def extract_cookie(response, name: str) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header.split(";", 1)[0][len(name) + 1 :]
    return None


def pair(env, client, secret: str | None = None):
    return client.get(f"/pair/{secret or env.room.owner_secret}")


# ---- 配对入口 ----


def test_pair_redirects_to_app_without_secret_in_location(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = pair(relay_env, client)

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    # 这一条是安全要求而不是体验偏好：secret 留在地址栏会进入浏览历史，
    # 也会被后续相对路径请求的 Referer 带出去。
    assert relay_env.room.owner_secret not in response.headers["location"]
    assert "location" in response.headers
    assert extract_cookie(response, SESSION_COOKIE)


def test_pair_sets_production_cookie_attributes(relay_env):
    """生产配置（Secure 开启）下的 Cookie 属性。

    刻意不用 cookie jar 发第二次请求——httpx 不会把 Secure Cookie 发回
    http://testserver。这里只断言响应头，所以不受该限制影响。
    """
    relay_env.add_room()
    with relay_env.client(RELAY_COOKIE_SECURE="1") as client:
        response = pair(relay_env, client)

    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=strict" in header or "SameSite=Strict" in header
    assert "Path=/" in header
    # 持久 Cookie：关掉浏览器后还在。没有 Max-Age 就是会话 Cookie，直接违反验收标准。
    assert f"Max-Age={SESSION_COOKIE_MAX_AGE}" in header


def test_pair_with_local_dev_settings_omits_secure(relay_env):
    relay_env.add_room()
    with relay_env.client(RELAY_COOKIE_SECURE="0") as client:
        response = pair(relay_env, client)

    assert "Secure" not in response.headers["set-cookie"]


def test_pair_reuses_existing_owner_session(relay_env):
    """收藏主人链接的人不应每点一次就多出一台设备。"""
    relay_env.add_room()
    with relay_env.client() as client:
        first = pair(relay_env, client)
        second = pair(relay_env, client)

    assert first.status_code == 303
    assert second.status_code == 303
    assert len(relay_env.read("SELECT id FROM devices")) == 1


def test_pair_unknown_secret_returns_404(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/pair/not-a-real-secret")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_pair_overlong_secret_returns_404(relay_env):
    """超长输入不应该走到哈希计算，但对外仍然是同一个 404，不暴露差异。"""
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/pair/" + "a" * 5000)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_room_without_any_room_yields_404(relay_env):
    """没执行过 init 时，任何 secret 都不该匹配到东西。"""
    with relay_env.client() as client:
        response = client.get("/pair/whatever")

    assert response.status_code == 404


# ---- 凭证存储 ----


def test_secret_and_session_token_are_never_stored_in_plaintext(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = pair(relay_env, client)
    token = extract_cookie(response, SESSION_COOKIE)
    assert token

    connection = relay_env.raw_connect()
    try:
        stored_secret = bytes(
            connection.execute("SELECT owner_secret_hash FROM rooms").fetchone()[0]
        )
        stored_token = bytes(
            connection.execute("SELECT token_hash FROM device_sessions").fetchone()[0]
        )
    finally:
        connection.close()

    secret_bytes = relay_env.room.owner_secret.encode()
    assert secret_bytes not in stored_secret
    assert token.encode() not in stored_token
    # 存的是哈希本身，不是"哈希的某种编码"——用同一个函数能对上。
    assert stored_token == hash_token(token)
    assert stored_secret == hash_token(relay_env.room.owner_secret)


# ---- 会话 ----


def test_session_survives_subsequent_requests(relay_env):
    """模拟"刷新、关浏览器、重启设备后仍登录"：只要 Cookie 在，就还在登录。"""
    relay_env.add_room()
    with relay_env.client() as client:
        pair(relay_env, client)
        first = client.get("/api/devices")
        second = client.get("/api/devices")

    assert first.status_code == 200
    assert second.status_code == 200


def test_request_without_session_is_401(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/api/devices")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_request_with_forged_cookie_is_401(relay_env):
    """随便编一个令牌不该通过。

    令牌用 ASCII：httpx 不允许非 ASCII 的 Cookie 值，那是测试工具的限制，
    不是应用的限制（浏览器会把非 ASCII 值百分号编码后再发）。
    """
    relay_env.add_room()
    with relay_env.cookie_client("forged-token-that-does-not-exist") as client:
        response = client.get("/api/devices")

    assert response.status_code == 401


def test_logout_revokes_session_and_is_idempotent(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        pair(relay_env, client)
        assert client.get("/api/devices").status_code == 200

        first = client.post("/api/session/logout")
        after = client.get("/api/devices")
        # 令牌已经被撤销，再次登出仍要成功——否则前端会卡在一个删不掉的登录态里
        second = client.post("/api/session/logout")

    assert first.status_code == 204
    assert after.status_code == 401
    assert second.status_code == 204
    assert relay_env.read("SELECT COUNT(*) AS n FROM devices") == [{"n": 1}]


def test_logout_redirects_html_clients(relay_env):
    """表单提交没有 JS 收 204，需要跳回首页。"""
    relay_env.add_room()
    with relay_env.client() as client:
        pair(relay_env, client)
        response = client.post("/api/session/logout", headers={"Accept": "text/html,*/*"})

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_logout_expires_cookie_on_client(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        pair(relay_env, client)
        response = client.post("/api/session/logout")

    header = response.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "max-age=0" in header.lower() or "expires=" in header.lower()


# ---- 来源校验（方案 9.1：写请求必须验证 Origin） ----


def test_cross_origin_write_is_rejected(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.post(
            "/api/session/logout", headers={"Origin": "https://evil.example"}
        )

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "origin_not_allowed"
    assert error["requestId"]


def test_null_origin_is_rejected(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.post("/api/session/logout", headers={"Origin": "null"})

    assert response.status_code == 403


def test_same_origin_and_missing_origin_are_allowed(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        same_origin = client.post("/api/session/logout", headers={"Origin": "http://testserver"})
        no_origin = client.post("/api/session/logout")

    assert same_origin.status_code == 204
    assert no_origin.status_code == 204


def test_origin_check_does_not_apply_to_safe_methods(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/healthz", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200


def test_origin_check_only_guards_api_paths(relay_env):
    """`/pair/` 是 GET，天然不受影响。"""
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get(
            f"/pair/{relay_env.room.owner_secret}",
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 303


# ---- 页面与响应头 ----


def test_pairing_response_sets_referrer_policy(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = pair(relay_env, client)

    assert response.headers["referrer-policy"] == "no-referrer"


def test_home_page_reflects_pairing_state(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        anonymous = client.get("/")
        pair(relay_env, client)
        paired = client.get("/")

    assert anonymous.status_code == 200
    assert "主人链接" in anonymous.text
    assert "已配对" in paired.text


def test_app_page_requires_pairing(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/app")

    assert response.status_code == 401
    assert "尚未配对" in response.text


def test_app_page_lists_devices_and_marks_current(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        pair(relay_env, client)
        response = client.get("/app")

    assert response.status_code == 200
    assert "设备列表" in response.text
    assert "当前设备" in response.text
    assert "rev=0" in response.text


def test_device_name_from_user_agent(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        client.get(
            f"/pair/{relay_env.room.owner_secret}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )

    assert relay_env.read("SELECT name FROM devices")[0]["name"] == "Windows 电脑"


def test_last_seen_is_refreshed_but_throttled(relay_env):
    """节流是刻意的：它不参与鉴权，没必要每个请求都写一次。"""
    relay_env.add_room()
    with relay_env.client() as client:
        pair(relay_env, client)
        # 把 last_seen_at 压到很久以前，制造"该刷新"的状态
        relay_env.execute("UPDATE devices SET last_seen_at = 0")
        client.get("/api/devices")
        refreshed = relay_env.read("SELECT last_seen_at FROM devices")[0]["last_seen_at"]
        client.get("/api/devices")
        again = relay_env.read("SELECT last_seen_at FROM devices")[0]["last_seen_at"]

    assert refreshed > 0
    # 第二次请求在 60 秒节流窗口内，不应再写
    assert again == refreshed
    assert abs(int(time.time()) - int(refreshed)) < 30
