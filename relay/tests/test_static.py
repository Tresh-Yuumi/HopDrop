"""静态资源与缓存策略（方案 9.1、11.2）。

这一层要守住两件事：

1. **`/static/` 是唯一不 `no-store` 的路径。** 其余全部 `no-store`，静态资源
   `no-cache` + ETag。两个方向都必须有测试：只测"静态资源可缓存"的话，哪天
   有人把例外写宽成"含 `static` 子串都放行"，API 响应就悄悄开始被缓存了。
2. **静态资源也必须带安全响应头。** 少了 `nosniff`，一个被当作脚本加载的
   `.css` 就能变成可执行内容——这是真实存在的攻击面，不是理论风险。
"""

from __future__ import annotations

import pytest
from conftest import RelayEnv

# 与 `app/api/pages.py::_APP_SCRIPTS` 一致。写死在这里是有意的：页面引用的
# 脚本清单发生变化时，这个测试必须一起被看见，而不是自动跟着改。
APP_SCRIPTS = (
    "/static/js/util.js",
    "/static/js/api.js",
    "/static/js/store.js",
    "/static/js/render.js",
    "/static/js/sync.js",
    "/static/js/actions.js",
    "/static/js/app.js",
)


@pytest.mark.parametrize("path", ["/static/app.css", *APP_SCRIPTS])
def test_every_referenced_asset_exists(relay_env: RelayEnv, path: str) -> None:
    """页面引用的每个资源都能取到 200。

    这条是防"改了文件名但没改引用"和"文件忘提交"的：它跑在应用层，所以
    能同时覆盖到挂载路径（`/static` ↔ `web/`）这一层映射。
    """
    with relay_env.client() as client:
        response = client.get(path)

    assert response.status_code == 200, f"{path} 取不到：{response.status_code}"
    assert response.content


def test_stylesheet_content_type_and_cache(relay_env: RelayEnv) -> None:
    with relay_env.client() as client:
        response = client.get("/static/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    # 没有构建步骤 → 文件名不带指纹 → 只能用 no-cache + ETag 协商
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag")


def test_script_content_type(relay_env: RelayEnv) -> None:
    with relay_env.client() as client:
        response = client.get("/static/js/util.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_static_asset_supports_conditional_request(relay_env: RelayEnv) -> None:
    """带上 ETag 再问一次应得 304 且无响应体。

    这是 `no-cache` 的兑现方式：每次回来问，但正常情况只传一个 304 头。
    """
    with relay_env.client() as client:
        first = client.get("/static/app.css")
        etag = first.headers["etag"]
        second = client.get("/static/app.css", headers={"If-None-Match": etag})

    assert second.status_code == 304
    assert not second.content


def test_static_still_carries_security_headers(relay_env: RelayEnv) -> None:
    with relay_env.client() as client:
        response = client.get("/static/app.css")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"]


def test_api_response_is_not_cached(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/api/healthz")

    assert response.headers["cache-control"] == "no-store"


def test_html_page_is_not_cached(relay_env: RelayEnv) -> None:
    """页面同样 `no-store`。外壳不随内容变化，但它带着会话字段。"""
    with relay_env.client() as client:
        response = client.get("/")

    assert response.headers["cache-control"] == "no-store"


def test_missing_asset_is_404(relay_env: RelayEnv) -> None:
    with relay_env.client() as client:
        response = client.get("/static/js/nope.js")

    assert response.status_code == 404


def test_static_mount_does_not_escape_its_directory(relay_env: RelayEnv) -> None:
    """路径穿越必须被挡住。

    用原始路径发请求，绕开客户端可能的规范化——Starlette 的 StaticFiles 自己
    会挡，这里断言它确实生效了。
    """
    with relay_env.client() as client:
        response = client.get("/static/../app/main.py")

    assert response.status_code in (400, 403, 404)
