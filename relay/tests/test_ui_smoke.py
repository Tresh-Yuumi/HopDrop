"""真实浏览器冒烟测试（M5 起）。

**这一层测的不是"文件对不对"，而是"页面真的跑起来了吗"。** 它补的是一段
前面所有测试都覆盖不到的空白：单元测试走 `TestClient`、静态检查扫字符串，
两者都不会执行一行 JavaScript，也不会让浏览器去解一次 CSS。

M5 的真机验证就是靠这种方式抓到三个 bug，三个都是"单测全绿、控制台无异常、
服务端日志干净"：

1. `util.el` 把 `data-boardId` 写成 `data-boardid` —— 区域标签点了没反应；
2. `util.el` 把 `className` 写成 `classname` —— 整份样式全部没生效，
   CSS 里那几十条规则一条都没命中，界面只是"看起来朴素"；
3. `sync.js` 的 `stopped` 初值为 `true` —— 首屏快照被守卫静默吃掉，`store`
   永远是空的，刷新后一片空白，而连接状态照样显示"已连接"。

三条的共同点是：**没有任何错误信息**。所以它们只能靠"真的跑一遍并断言结果"
来兜住，静态检查再密也兜不住——第 2 条发生时，那条"外壳用到的 class 在 CSS
里都有定义"的检查是全绿的，因为名字一个字都没错。

默认跳过：它需要一个真实浏览器，耗时也在秒级以上，不该拖慢日常的测试循环。
改动前端之后手动跑一次：

    RELAY_UI_E2E=1 ../.venv/Scripts/python.exe -m pytest tests/test_ui_smoke.py -q

浏览器路径默认按常见位置找 Chrome/Edge，也可以用 `RELAY_CHROME` 指定。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import RelayEnv

E2E_ENABLED = os.environ.get("RELAY_UI_E2E") == "1"
# 驱动 Chrome 的调试端口要一个 WebSocket 客户端。它在 uvicorn[standard] 里
# 就有，但测试不该依赖"某个运行依赖恰好带了它"，所以显式判断一次。
HAS_WEBSOCKETS = importlib.util.find_spec("websockets") is not None

CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
    Path("/usr/bin/chromium-browser"),
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
)

# 浏览器里每一步的期望。收集成列表而不是逐条 assert，是为了让失败时**一次
# 看全所有不符项**：冒烟测试的价值在于"现在到底哪几处断了"，只报第一条会
# 让人来回跑很多遍。
Check = tuple[str, bool, str]


def chrome_path() -> Path | None:
    override = os.environ.get("RELAY_CHROME")
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


pytestmark = [
    pytest.mark.skipif(not E2E_ENABLED, reason="需要真实浏览器：设 RELAY_UI_E2E=1 才跑"),
    pytest.mark.skipif(
        chrome_path() is None,
        reason="没找到可用的 Chrome/Edge（可用 RELAY_CHROME 指定）",
    ),
    pytest.mark.skipif(not HAS_WEBSOCKETS, reason="驱动 Chrome 调试端口需要 websockets 包"),
]


# ----------------------------------------------------------------- 极简 HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _Device:
    """页面之外的第二台设备。只用来发起"另一台设备"的写入。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie: str | None = None
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "*/*"}
        if data:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            response = self._opener.open(request)
        except urllib.error.HTTPError as error:
            response = error
        try:
            payload = response.read()
            for value in response.headers.get_all("Set-Cookie") or []:
                self.cookie = value.split(";", 1)[0]
            return response.status, payload
        finally:
            response.close()

    def json(self, method: str, path: str, body: dict | None = None):
        status, payload = self.request(method, path, body)
        return status, (json.loads(payload) if payload else {})


# ----------------------------------------------------------------- CDP 客户端


class _Chrome:
    """只用到 Runtime / Page / Log / Network 四个域的 CDP 客户端。

    不引第三方驱动库是刻意的：这里要的只是"把页面打开、执行表达式、收集
    控制台错误"，为此拉一个浏览器自动化框架进来，等于为了二十行功能装一套
    会自己升级、自己漂移的依赖。
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.ws = None
        self.seq = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.console_errors: list[str] = []
        self.responses: list[tuple[int, str]] = []

    def _ws_url(self) -> str:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=2
                ) as stream:
                    for target in json.load(stream):
                        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                            return target["webSocketDebuggerUrl"]
            except Exception:  # noqa: BLE001 - 端口还没起来，继续等
                pass
            time.sleep(0.3)
        raise RuntimeError("Chrome 调试端口没有就绪")

    async def connect(self) -> None:
        import websockets

        self.ws = await websockets.connect(self._ws_url(), max_size=32 * 1024 * 1024)
        asyncio.ensure_future(self._reader())
        for domain in ("Runtime.enable", "Page.enable", "Log.enable", "Network.enable"):
            await self.call(domain)

    async def _reader(self) -> None:
        async for raw in self.ws:
            message = json.loads(raw)
            method = message.get("method")
            if "id" in message:
                future = self.pending.pop(message["id"], None)
                if future is not None and not future.done():
                    future.set_result(message)
            elif method == "Runtime.exceptionThrown":
                details = message["params"]["exceptionDetails"]
                text = details.get("exception", {}).get("description") or details.get("text")
                self.console_errors.append(f"未捕获异常：{text}")
            elif method == "Log.entryAdded":
                entry = message["params"]["entry"]
                if entry.get("level") == "error":
                    # 带上 URL —— 网络类条目的正文里没有 URL，只有一句
                    # "responded with a status of 404"，无从下手。
                    self.console_errors.append(
                        f"{entry.get('source')}：{entry.get('text')} <{entry.get('url') or ''}>"
                    )
            elif method == "Runtime.consoleAPICalled":
                if message["params"].get("type") == "error":
                    args = [
                        str(item.get("value", item.get("description", "")))
                        for item in message["params"]["args"]
                    ]
                    self.console_errors.append("console.error：" + " ".join(args))
            elif method == "Network.responseReceived":
                response = message["params"]["response"]
                self.responses.append((int(response.get("status", 0)), response.get("url", "")))

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.seq += 1
        call_id = self.seq
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        await self.ws.send(
            json.dumps({"id": call_id, "method": method, "params": params or {}})
        )
        message = await asyncio.wait_for(future, timeout=30)
        if "error" in message:
            raise RuntimeError(f"{method} 失败：{message['error']}")
        return message.get("result", {})

    async def evaluate(self, expression: str):
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            # 真正的错误信息在 `exception.description` 里，`text` 只有一句
            # "Uncaught"——只报 `text` 的话，失败信息等于什么都没说，还得回头
            # 翻是哪一句表达式炸的。所以把表达式也带上。
            details = result["exceptionDetails"]
            reason = (details.get("exception") or {}).get("description") or details.get("text")
            snippet = " ".join(expression.split())[:160]
            raise RuntimeError(f"页面里求值失败：{reason}\n  表达式：{snippet}")
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, *, timeout: float = 15) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.evaluate(expression):
                    return True
            except Exception:  # noqa: BLE001 - 导航期间上下文会被销毁，属正常
                pass
            await asyncio.sleep(0.2)
        return False


# ----------------------------------------------------------------- 服务进程


class _Server:
    def __init__(self, data_dir: Path, port: int) -> None:
        self.data_dir = data_dir
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> "_Server":
        env = {
            **os.environ,
            "RELAY_DATA_DIR": str(self.data_dir),
            "RELAY_ADDR": f"127.0.0.1:{self.port}",
            # 本地是 http，Cookie 不能带 Secure，否则浏览器不回传。
            "RELAY_COOKIE_SECURE": "0",
            "PYTHONUNBUFFERED": "1",
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "app"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(f"服务进程提前退出：\n{output}")
            try:
                with urllib.request.urlopen(f"{self.base}/healthz", timeout=2) as response:
                    if response.status == 200:
                        return self
            except Exception:  # noqa: BLE001 - 还没起来，继续等
                pass
            time.sleep(0.25)
        raise RuntimeError("服务在 30 秒内没有就绪")

    def __exit__(self, *_exc) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def stop_for_logs(self) -> str:
        """取服务端输出，用于在断言失败时补上上下文。"""
        if self.process is None or self.process.stdout is None:
            return ""
        self.process.terminate()
        try:
            return self.process.communicate(timeout=10)[0] or ""
        except Exception:  # noqa: BLE001 - 拿不到日志不该盖住真正的失败
            return ""


# ----------------------------------------------------------------- 测试本体


async def _drive(base: str, secret: str, work_dir: Path) -> list[Check]:
    checks: list[Check] = []
    chrome_path_value = str(chrome_path())
    profile = Path(tempfile.mkdtemp(prefix="hopdrop-e2e-", dir=str(work_dir)))
    port = free_port()
    browser = subprocess.Popen(
        [
            chrome_path_value,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--window-size=1280,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    device = _Device(base)
    run_id = str(int(time.time()))
    try:
        status, _ = device.request("GET", f"/pair/{secret}")
        checks.append(("第二台设备配对成功（303）", status == 303, str(status)))

        page = _Chrome(port)
        await page.connect()

        # ---- 首页：未配对时是纯静态说明页
        await page.call("Page.navigate", {"url": base + "/"})
        home_ready = await page.wait_for(
            "document.readyState === 'complete'"
            " && document.body.innerText.includes('保留规则')"
        )
        checks.append(("首页载入并渲染保留规则", home_ready, ""))
        checks.append(
            (
                "首页没有任何脚本（未配对时是纯静态页）",
                await page.evaluate("document.querySelectorAll('script').length") == 0,
                "",
            )
        )

        # ---- 配对：链接把浏览器带到应用页
        await page.call("Page.navigate", {"url": f"{base}/pair/{secret}"})
        landed = await page.wait_for(
            "location.pathname === '/app' && document.getElementById('hd-app') !== null"
        )
        checks.append(("配对链接把浏览器带到 /app", landed, ""))

        # 会话 Cookie 是 HttpOnly 的：`document.cookie` 里看不到它才是对的，
        # 所以只能验证"浏览器真的带上了它"——从页面里发一次同源请求。
        checks.append(
            (
                "会话 Cookie 生效（HttpOnly：JS 读不到，但同源请求带得上）",
                bool(
                    await page.evaluate(
                        "fetch('/api/snapshot', {credentials: 'same-origin'})"
                        ".then(function (r) { return r.ok; })"
                    )
                ),
                "",
            )
        )
        checks.append(
            (
                "JS 侧读不到会话令牌（HttpOnly 生效）",
                bool(
                    await page.evaluate(
                        "!document.cookie.split(';').some(function (c) {"
                        "return c.trim().indexOf('relay_session') === 0;})"
                    )
                ),
                "",
            )
        )

        # ---- 首屏基线：**必须真的去拉了 /api/snapshot**
        # 直接看网络。区域标签也可能由 WS 推来一个就长一个，从 DOM 反推不出来
        # ——`sync.js` 的 `stopped` 初值为 true 时就是这样：快照被静默吃掉，
        # 界面照样会慢慢出现区域，连接状态照样显示已连接。
        boards_rendered = await page.wait_for(
            "document.querySelectorAll('#hd-boards [data-board-id]').length >= 2"
        )
        checks.append(("两个初始区域渲染进区域栏", boards_rendered, ""))
        snapshot_200 = [
            url for (status_, url) in page.responses
            if url.endswith("/api/snapshot") and status_ == 200
        ]
        checks.append(
            ("首屏确实请求了 /api/snapshot 且拿到 200", bool(snapshot_200), str(snapshot_200))
        )
        store_state = await page.evaluate(
            "JSON.stringify({ready: window.HD.store.state.ready,"
            " boards: window.HD.store.state.boards.length,"
            " active: !!window.HD.store.state.activeBoardId})"
        )
        checks.append(
            ("store 已用快照建立基线（ready、有区域、有激活区域）",
             store_state == '{"ready":true,"boards":2,"active":true}', store_state)
        )
        # 连接状态是异步的：boot 的顺序是"快照 → 设备列表 → 才开 WebSocket"，
        # 所以快照画完之后会先短暂停在重连中。一次性判断必然撞上这个窗口。
        checks.append(
            (
                "连接状态最终变为已连接",
                await page.wait_for(
                    "document.getElementById('hd-conn').dataset.state === 'online'"
                ),
                str(await page.evaluate("document.getElementById('hd-conn').textContent")),
            )
        )

        # ---- 样式真的生效了吗
        # 分两条查，因为它们断的原因不同、修法也不同：
        #
        # 1. `class` 属性本身在不在、值对不对 —— `className` 写成 `classname` 时
        #    这一条直接报 `null`，最直白；
        # 2. 浏览器有没有把 CSS 规则套上去 —— 属性名对了但样式没生效（比如选择器
        #    被写错、文件没加载）时只有这一条能发现。
        #
        # 只查第 2 条的话，失败详情会是"2px"这种莫名其妙的值：丢掉 class 的按钮
        # 会退回浏览器默认的 2px 边框，看着像"有样式"。
        boards_class = await page.evaluate(
            "(function () {"
            " var n = document.querySelector('#hd-boards [data-board-id]');"
            " return n ? n.getAttribute('class') : 'no-element';"
            "})()"
        )
        checks.append(
            (
                "JS 建的元素用的是真 class 属性（区域标签的 class 是 boards__item）",
                boards_class == "boards__item",
                str(boards_class),
            )
        )
        # 表达式写成 IIFE 并在找不到元素时返回 `no-element`：**样式没生效时，
        # 靠 class 的选择器本来就找不到元素**，直接 `getComputedStyle(null)`
        # 会抛异常、把整条冒烟测试崩掉，看不到其它已经断掉的地方。冒烟测试的
        # 价值恰恰是"一次报全"，所以这里必须返回一个值。
        boards_border = await page.evaluate(
            "(function () {"
            " var n = document.querySelector('#hd-boards [data-board-id]');"
            " return n ? getComputedStyle(n).borderTopWidth : 'no-element';"
            "})()"
        )
        checks.append(
            (
                "浏览器真的把 app.css 套上了（区域标签是 1px 边框，不是默认的 2px）",
                boards_border == "1px",
                str(boards_border),
            )
        )
        # `data-board-id` 能读到，且值就是区域 id —— dataset 那次不匹配的形态是
        # `getAttribute('data-board-id')` 返回 null，标签点了没反应。
        checks.append(
            (
                "区域标签的 data-board-id 能被 getAttribute 读到（点击才有目标）",
                await page.evaluate(
                    "var n = document.querySelector('#hd-boards [data-board-id]');"
                    "!!n && n.getAttribute('data-board-id').length > 0"
                ),
                "",
            )
        )

        # ---- 输入框：字数统计随输入变化（纯前端行为）
        await page.evaluate(
            "var el = document.getElementById('hd-input');"
            "el.value = '测试文本';"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
        )
        count_text = await page.evaluate("document.getElementById('hd-count').textContent")
        checks.append(("输入后字数提示更新（4 个汉字 = 12 字节）", "12" in str(count_text), str(count_text)))
        await page.evaluate(
            "var el = document.getElementById('hd-input');"
            "el.value = '';"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
        )

        # ---- 跨设备：另一台设备写入，浏览器不刷新就该出现
        status, body = device.json("POST", "/api/boards", {"name": f"冒烟区 {run_id}"})
        checks.append(("另一台设备建区 201", status == 201, str(status)))
        board_id = body["board"]["id"]

        checks.append(
            (
                "新区域通过 WebSocket 推送自动出现（未刷新）",
                await page.wait_for(
                    "!!document.querySelector('#hd-boards [data-board-id=\""
                    + board_id
                    + "\"]')"
                ),
                "",
            )
        )

        phone_text = f"来自另一台设备 {run_id}"
        status, _ = device.json(
            "POST",
            f"/api/boards/{board_id}/notes",
            {"content": phone_text, "mutationId": f"smoke-phone-{run_id}"},
        )
        checks.append(("另一台设备发消息 201", status == 201, str(status)))
        await page.evaluate(
            "document.querySelector('#hd-boards [data-board-id=\""
            + board_id
            + "\"]').click()"
        )
        checks.append(
            (
                "切到该区域后消息内容渲染出来（未刷新）",
                await page.wait_for(
                    "document.getElementById('hd-notes').innerText.includes('"
                    + phone_text
                    + "')"
                ),
                "",
            )
        )
        # 幂等重放：同一个 mutationId 再发一次，应当是 200 且不新增
        status, _ = device.json(
            "POST",
            f"/api/boards/{board_id}/notes",
            {"content": phone_text, "mutationId": f"smoke-phone-{run_id}"},
        )
        checks.append(("同一 mutationId 重放得到 200（不是 201）", status == 200, str(status)))
        _, snapshot = device.json("GET", "/api/snapshot")
        in_board = next(b for b in snapshot["boards"] if b["id"] == board_id)
        checks.append(("重放没有写入第二条", len(in_board["notes"]) == 1, str(len(in_board["notes"]))))

        # 消息卡片也要有样式（`.note` 有 1px 边框）——JS 建的元素里最常出现的那类。
        # 同样的，选择器本身依赖 class，取不到就返回哨兵值而不是抛异常。
        note_border = await page.evaluate(
            "(function () {"
            " var n = document.querySelector('#hd-notes .note');"
            " return n ? getComputedStyle(n).borderTopWidth : 'no-element';"
            "})()"
        )
        checks.append(
            ("消息卡片真的套上了 CSS（消息卡有 1px 边框）", note_border == "1px", str(note_border))
        )

        # ---- 浏览器自己发一条，另一台设备要能读到
        self_sent = f"来自浏览器 {run_id}"
        await page.evaluate(
            "var el = document.getElementById('hd-input');"
            "el.value = '" + self_sent + "';"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
            "document.getElementById('hd-send').click();"
        )
        checks.append(
            (
                "浏览器发出的消息上屏",
                await page.wait_for(
                    "document.getElementById('hd-notes').innerText.includes('"
                    + self_sent
                    + "')"
                ),
                "",
            )
        )
        _, snapshot = device.json("GET", "/api/snapshot")
        contents = [
            note["content"]
            for board in snapshot["boards"]
            if board["id"] == board_id
            for note in board["notes"]
        ]
        checks.append(("另一台设备读到了浏览器发出的内容", self_sent in contents, str(contents)))
        checks.append(
            ("两条消息顺序正确（新消息在前）", contents[:2] == [self_sent, phone_text], str(contents[:2]))
        )

        # ---- 编辑标记由推送驱动
        _, snapshot = device.json("GET", "/api/snapshot")
        in_board = next(b for b in snapshot["boards"] if b["id"] == board_id)
        note_id = next(n["id"] for n in in_board["notes"] if n["content"] == phone_text)
        edited_text = phone_text + "（已改）"
        status, _ = device.json("PATCH", f"/api/notes/{note_id}", {"content": edited_text})
        checks.append(("另一台设备编辑成功", status == 200, str(status)))
        checks.append(
            (
                "浏览器上出现「已编辑」标记（推送驱动，未刷新）",
                await page.wait_for(
                    "document.getElementById('hd-notes').innerText.includes('已编辑')"
                ),
                "",
            )
        )

        # ---- 设置抽屉：设备列表由前端拉取渲染
        await page.evaluate("document.getElementById('hd-settings-toggle').click()")
        checks.append(
            (
                "设置抽屉里的设备列表由前端拉取并渲染",
                await page.wait_for("document.querySelectorAll('#hd-devices li').length >= 2"),
                "",
            )
        )
        checks.append(
            (
                "当前设备被标出来",
                bool(
                    await page.evaluate(
                        "document.getElementById('hd-devices')"
                        ".innerText.includes('当前设备')"
                    )
                ),
                "",
            )
        )
        await page.evaluate("document.getElementById('hd-settings-close').click()")

        # ---- 刷新：走一遍纯快照路径，并回到刷新前停留的区域
        await page.call("Page.reload", {"ignoreCache": True})
        checks.append(
            (
                "刷新后通过快照恢复出同一个区域的内容",
                await page.wait_for(
                    "document.getElementById('hd-notes')"
                    " && document.getElementById('hd-notes').innerText.includes('"
                    + self_sent
                    + "')"
                ),
                "",
            )
        )
        active_after = await page.evaluate("window.HD.store.state.activeBoardId")
        checks.append(
            ("刷新后回到刷新前停留的区域（不是跳回第一个）", active_after == board_id, str(active_after))
        )
        checks.append(
            (
                "刷新后连接重新建立",
                await page.wait_for(
                    "document.getElementById('hd-conn').dataset.state === 'online'"
                ),
                "",
            )
        )
        checks.append(
            (
                "刷新后编辑后的正文仍在（来自快照）",
                bool(
                    await page.evaluate(
                        "document.getElementById('hd-notes').innerText.includes('"
                        + edited_text
                        + "')"
                    )
                ),
                "",
            )
        )

        # ---- 控制台必须干净
        checks.append(
            ("全程无控制台异常或错误", not page.console_errors, " | ".join(page.console_errors[:4]))
        )
        return checks
    finally:
        browser.terminate()
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            browser.kill()
        shutil.rmtree(profile, ignore_errors=True)


def test_ui_smoke(tmp_path: Path) -> None:
    """起真服务、开真浏览器，把跨设备文本闭环走一遍。"""
    data_dir = tmp_path / "data"
    env = RelayEnv(data_dir=data_dir)
    room = env.add_room("冒烟房间")

    port = free_port()
    with _Server(data_dir, port) as server:
        checks = asyncio.run(_drive(server.base, room.owner_secret, tmp_path))
        if any(not ok for _, ok, _ in checks):
            # 失败时把服务端输出一起带上：不然只有浏览器侧的现象，看不出
            # 是前端没发请求，还是服务端拒了。
            logs = server.stop_for_logs()
        else:
            logs = ""

    failed = [(label, detail) for label, ok, detail in checks if not ok]
    report = "\n".join(
        f"  {'OK ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else "")
        for label, ok, detail in checks
    )
    assert not failed, f"浏览器冒烟测试有 {len(failed)} 项不符：\n{report}\n\n服务端输出：\n{logs}"
