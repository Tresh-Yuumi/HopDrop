"""部署件的测试。

这一组测试的对象是 `deploy/` 里的脚本与单元文件本身，不是服务功能。它要防的是
两类事故，都是"看一眼觉得没问题、出事时才发现"的类型：

1. **多处配置悄悄漂移**。同一个值出现在 Caddyfile、systemd unit、env 模板、
   应用配置里，改了一处忘了另一处。测试用"从一处推导另一处"的方式锁住它们，
   而不是把常量抄第二遍——抄第二遍的测试在漂移时也会一起漂。
2. **幂等性与不覆盖假设被打破**。`install.sh` 会被反复执行，其中"已存在的
   relay.env 不覆盖"是一条硬约定：里面有部署者填的域名与备案号。这条约定靠
   文档记住是靠不住的，所以这里真的把脚本跑起来验证。

`install.sh` 提供了 `--prefix` 与 `--no-system`，正是因为要让它能在开发机上
被真实执行——只做语法检查和字符串断言的话，上面第 2 类问题一个都测不到。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_DIR / "deploy"
RELAY_DIR = REPO_DIR / "relay"

BASH = shutil.which("bash")
CURL = shutil.which("curl")

requires_bash = pytest.mark.skipif(BASH is None, reason="需要 bash")
requires_curl = pytest.mark.skipif(CURL is None, reason="健康检查脚本需要 curl")

SHELL_SCRIPTS = sorted(DEPLOY_DIR.glob("*.sh"))

# Windows 上的 chmod 不真实生效，涉及权限位的断言只在类 Unix 上跑。
POSIX = os.name == "posix"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def msys_path(path: object) -> str:
    r"""把 Windows 路径转成 Git Bash 看得懂的形式。

    两层原因：一是 MSYS 下的路径转换（`C:\a\b` → `/c/a/b`）能避免个别工具
    在混合路径 `C:\a\b/opt/relay` 上出问题；二是 Git Bash 的 bash 收到
    `C:\...` 形式的脚本路径时会当成相对路径去找，直接找不到文件。
    """
    text = str(path)
    if os.name == "nt" and shutil.which("cygpath"):
        result = subprocess.run(
            ["cygpath", "-u", text], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return text


def run_bash(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    assert BASH is not None
    return subprocess.run(
        [BASH, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


# --------------------------------------------------------------- 脚本本身的静态检查


@requires_bash
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_script_has_valid_syntax(script: Path) -> None:
    result = run_bash(["-n", msys_path(script)])
    assert result.returncode == 0, f"{script.name} 语法错误：{result.stderr}"


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_script_uses_lf_line_endings(script: Path) -> None:
    r"""这些脚本在 Windows 上编辑、在 Linux 上执行。

    CRLF 会让 shebang 变成 `#!/usr/bin/env bash\r`，Linux 报
    `bad interpreter: No such file or directory`——报错信息完全指不到真正的原因。
    本机有 Git Bash 兜着，所以这个坑只在真实服务器上暴露。
    """
    raw = script.read_bytes()
    assert b"\r\n" not in raw, f"{script.name} 含 CRLF 换行，在 Linux 上执行时会失败"
    assert raw.startswith(b"#!/usr/bin/env bash")


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_script_declares_strict_mode(script: Path) -> None:
    """至少要开一个 `set -`。

    这里不强制 `-e`：健康检查脚本必须自己处理"请求失败"这条正常路径，
    开了 errexit 反而要在每个分支上写 `|| true`。它的取舍由下面那条测试
    单独锁住。
    """
    body = read(script)
    assert re.search(r"^set -[a-z]", body, flags=re.MULTILINE), (
        f"{script.name} 没有开启严格模式"
    )


def test_healthcheck_deliberately_omits_errexit() -> None:
    """健康检查脚本不开 errexit，且必须把理由写在文件里。

    这条断言存在的意义是防止有人"顺手"给它加上 `-e`：加上之后第一个 curl
    失败就会终止脚本，degraded 与不可达两条分支都走不到——而那正是这个脚本
    存在的全部理由。
    """
    body = read(DEPLOY_DIR / "hopdrop-healthcheck.sh")
    assert "set -uo pipefail" in body
    assert not re.search(r"^set -[a-z]*e", body, flags=re.MULTILINE), (
        "healthcheck 不应开启 errexit，见该测试的说明"
    )
    assert "退出码即健康状态" in body, "文件里要说明为什么不加 -e"


# --------------------------------------------------------------- systemd 单元


def _effective_lines(body: str) -> list[str]:
    """去掉注释与空行后剩下的内容。

    断言"文件里不含某个字符串"时必须走这里：这些文件的注释里到处都在解释
    "为什么不要写 --workers""为什么默认是 :80"，直接扫全文会把说明文字当成
    违规代码，于是只能把注释删掉来让测试变绿——那正好丢掉了最有价值的部分。
    """
    return [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_relay_unit_never_uses_workers() -> None:
    """方案 2.2 第 1 条：多 worker 会分裂 WebSocket 连接表与写锁。

    这是本项目里唯一"加了它就会静默坏掉、且症状极难定位"的配置，所以让它在
    测试里显式失败，而不是靠一句注释警告。
    """
    lines = _effective_lines(read(DEPLOY_DIR / "relay.service"))
    body = "\n".join(lines)

    assert "--workers" not in body
    exec_lines = [line for line in lines if line.startswith("ExecStart=")]
    assert len(exec_lines) == 1, "relay.service 里应恰好有一条 ExecStart"
    assert "--host 127.0.0.1 --port 8080" in exec_lines[0], (
        "服务只应监听回环，对外由 Caddy 反代"
    )


def test_relay_unit_paths_match_install_script() -> None:
    """单元文件里的绝对路径必须和 install.sh 生成的目录一致。

    这两处一旦不一致，症状是服务起不来但报错指向 `EnvironmentFile=/etc/relay/relay.env
    (No such file)`，看起来像配置没生成，实际是路径写错了。
    """
    unit = read(DEPLOY_DIR / "relay.service")
    install = read(DEPLOY_DIR / "install.sh")

    assert "WorkingDirectory=/opt/relay" in unit
    assert "EnvironmentFile=/etc/relay/relay.env" in unit
    assert "ReadWritePaths=/var/lib/relay" in unit

    for path in ("/opt/relay", "/etc/relay/relay.env", "/var/lib/relay"):
        # install.sh 通过变量拼出这些路径，所以断言的是变量赋值本身。
        assert path.split("/")[-1] in install, f"install.sh 里找不到 {path} 的对应变量"

    assert "APP_DIR=" in install and "/opt/relay" in install
    assert "ENV_FILE=" in install and "/etc/relay" in install
    assert "STATE_DIR=" in install and "/var/lib/relay" in install


def test_relay_unit_hardens_the_service() -> None:
    """ProtectSystem=strict 把 /opt/relay 变只读，所以代码目录被保护住了。

    这条同时也解释了下一条为什么必须存在。
    """
    unit = read(DEPLOY_DIR / "relay.service")
    for directive in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "RestrictSUIDSGID=true",
    ):
        assert directive in unit, f"缺少加固项 {directive}"
    assert "UMask=0077" in unit


def test_relay_unit_disables_pycache_writes() -> None:
    """ProtectSystem=strict 下写不了 __pycache__，显式关掉免得白白尝试。"""
    unit = read(DEPLOY_DIR / "relay.service")
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in unit


def test_healthcheck_timer_is_installed_by_install_script() -> None:
    install = read(DEPLOY_DIR / "install.sh")
    for name in (
        "hopdrop-healthcheck.sh",
        "hopdrop-healthcheck.service",
        "hopdrop-healthcheck.timer",
    ):
        assert (DEPLOY_DIR / name).is_file(), f"缺少 {name}"
        assert name in install, f"install.sh 没有安装 {name}"
    assert "enable --now hopdrop-healthcheck.timer" in install


# --------------------------------------------------------------- Caddyfile


def test_caddyfile_falls_back_to_plain_http() -> None:
    r"""域名没配时降级到 :80，而不是拿一个占位域名去反复申请证书。

    占位默认值（比如 relay.example.com）的后果是 Caddy 持续重试 ACME、日志被
    刷满，而"域名没配"这条真正的原因一条都不提。
    """
    body = read(DEPLOY_DIR / "Caddyfile")
    assert re.search(r"^\{\$RELAY_DOMAIN::80\}\s*\{", body, flags=re.MULTILINE), (
        "站点地址应当是 {$RELAY_DOMAIN::80}"
    )
    assert "relay.example.com" not in "\n".join(_effective_lines(body))


def test_caddyfile_body_limit_exceeds_application_limit() -> None:
    """反代的请求体上限必须大于应用的单文件上限。

    反过来会让应用永远收不到"文件过大"这个自己的错误码，用户看到的是反代的
    413 纯文本，而不是本项目的错误信封。
    """
    from app.config import MAX_FILE_BYTES_HARD_LIMIT

    body = read(DEPLOY_DIR / "Caddyfile")
    match = re.search(r"max_size\s+(\d+)\s*MB", body)
    assert match, "Caddyfile 里找不到 request_body max_size"

    limit_bytes = int(match.group(1)) * 1024 * 1024
    assert limit_bytes > MAX_FILE_BYTES_HARD_LIMIT, (
        f"反代上限 {limit_bytes} 不大于应用上限 {MAX_FILE_BYTES_HARD_LIMIT}"
    )


def test_caddyfile_has_no_hardcoded_deployment_details() -> None:
    """Caddyfile 必须能被 install.sh 无条件覆盖。

    一旦它含域名/证书路径这类部署专属信息，"随发布自动更新"就不再成立，
    于是所有人都开始手工维护服务器上的那一份，仓库这份变成摆设。
    """
    effective = "\n".join(_effective_lines(read(DEPLOY_DIR / "Caddyfile")))
    # 域名只以环境变量形式出现，且只出现一次（就是那个站点块）
    assert effective.count("{$RELAY_DOMAIN") == 1
    assert "tls internal" not in effective
    assert "/etc/ssl" not in effective


def test_caddyfile_disables_access_log() -> None:
    """访问日志里会带房间与区域标识，默认丢弃。"""
    body = read(DEPLOY_DIR / "Caddyfile")
    assert "output discard" in body


# --------------------------------------------------------------- 环境变量模板


def test_env_example_covers_every_config_key() -> None:
    """模板必须覆盖 config.py 读的每一个 RELAY_* 键。

    从源码里正则提取键名，而不是在这里维护第二份清单：漏掉的键在服务器上会
    静默取代码默认值，而默认值是"面向本地开发"的，正是不该出现在生产的那一组。
    """
    source = read(RELAY_DIR / "app" / "config.py")
    keys = set(re.findall(r'_(?:raw|int|bool|float)\(\s*env,\s*"(RELAY_[A-Z0-9_]+)"', source))
    assert len(keys) > 20, "提取到的配置键太少，正则可能失效了"

    template = read(RELAY_DIR / "relay.env.example")
    declared = set(re.findall(r"^(RELAY_[A-Z0-9_]+)=", template, flags=re.MULTILINE))

    missing = sorted(keys - declared)
    assert not missing, f"relay.env.example 缺少这些配置项：{missing}"


def test_env_example_only_declares_known_keys() -> None:
    """反过来：模板里不该出现 config.py 不认识的键。

    允许两个例外，它们不归 config.py 管：
    - RELAY_DOMAIN 给 Caddy 用；
    - RELAY_ADDR 是被 config.py 读的，已在上面覆盖（这里显式列出以免误判）。
    """
    source = read(RELAY_DIR / "app" / "config.py")
    known = set(re.findall(r'_(?:raw|int|bool|float)\(\s*env,\s*"(RELAY_[A-Z0-9_]+)"', source))

    template = read(RELAY_DIR / "relay.env.example")
    declared = set(re.findall(r"^(RELAY_[A-Z0-9_]+)=", template, flags=re.MULTILINE))

    extra = declared - known - {"RELAY_DOMAIN"}
    assert not extra, f"relay.env.example 里有 config.py 不认识的键：{sorted(extra)}"


def test_env_example_keeps_production_safe_defaults() -> None:
    """模板是生产用的，几个安全开关的默认值不能是开发取值。"""
    template = read(RELAY_DIR / "relay.env.example")
    assert re.search(r"^RELAY_COOKIE_SECURE=1$", template, flags=re.MULTILINE), (
        "生产模板里 RELAY_COOKIE_SECURE 必须是 1"
    )
    assert re.search(r"^RELAY_DATA_DIR=/var/lib/relay$", template, flags=re.MULTILINE)


# --------------------------------------------------------------- install.sh 的行为


@pytest.fixture
def drill_prefix(tmp_path: Path) -> Path:
    """搭出一棵和服务器同构的目录树。

    关键是 `/opt/relay` 下直接是 `app/`、`web/`，不是 `relay/app/`——
    这正是 release.sh 铺代码的方式，也是 install.sh 预检会拦的那个错误。
    """
    root = tmp_path / "drill"
    app_dir = root / "opt" / "relay"
    app_dir.mkdir(parents=True)
    for name in ("app", "web", "migrations"):
        shutil.copytree(RELAY_DIR / name, app_dir / name)
    shutil.copy2(RELAY_DIR / "requirements.txt", app_dir / "requirements.txt")
    shutil.copy2(RELAY_DIR / "relay.env.example", app_dir / "relay.env.example")
    shutil.copytree(DEPLOY_DIR, app_dir / "deploy")
    return root


def run_install(prefix: Path, *extra: str) -> subprocess.CompletedProcess:
    return run_bash(
        [
            msys_path(DEPLOY_DIR / "install.sh"),
            "--prefix",
            msys_path(prefix),
            *extra,
        ]
    )


def tree_digest(root: Path) -> dict[str, str]:
    """目录里每个文件的内容指纹。用来断言"第二次运行没有改动任何文件"。"""
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


@requires_bash
def test_install_drill_lifecycle(drill_prefix: Path) -> None:
    """把 install.sh 在服务器上的时间线完整走一遍。

    四个阶段合并成一个测试是有意的：每一步都依赖上一步留下的状态（第二次运行
    要看到第一次生成的文件，"不覆盖"要建立在文件已存在之上）。拆成四个测试就
    要把 install.sh 跑八遍——它在 Windows 上一次约 7 秒（上百次外部命令调用），
    拆开的代价是测试套件慢到没人愿意在提交前跑它。

    阶段：首次部署 → 再部署一次（幂等）→ 改配置再部署（不覆盖）→ --check（不写）。
    """
    # ---------- 阶段 1：首次部署 ----------
    first = run_install(drill_prefix)
    assert first.returncode == 0, first.stdout + first.stderr

    # 断言用 Windows 侧的路径（drill_prefix 只在交给 bash 时才转成 MSYS 形式）：
    # 把 `/c/Users/...` 交给 pathlib 会被当成"当前盘符下的相对路径"，永远不存在。
    for rel in (
        "etc/relay/relay.env",
        "etc/systemd/system/relay.service",
        "etc/systemd/system/caddy.service.d/10-hopdrop.conf",
        "etc/caddy/Caddyfile",
        "etc/cron.d/hopdrop-backup",
        "etc/systemd/system/hopdrop-healthcheck.timer",
        "usr/local/bin/hopdrop-backup",
        "usr/local/bin/hopdrop-healthcheck",
        "var/lib/relay/files",
        "var/lib/relay/thumbs",
        "var/lib/relay/backup",
    ):
        assert (drill_prefix / rel).exists(), f"install.sh 没有生成 {rel}"

    if POSIX:  # pragma: no cover - 仅在 Linux 测试机上走
        assert (drill_prefix / "var/lib/relay").stat().st_mode & 0o777 == 0o700, (
            "数据目录里有房间数据库与上传的文件，必须是私有的"
        )

    after_first = tree_digest(drill_prefix)

    # ---------- 阶段 2：再跑一次，必须幂等 ----------
    # 幂等不只是"不报错"：如果每次都重写 relay.env，部署者填的域名与备案号
    # 会在下一次发布时被模板悄悄覆盖回去。
    second = run_install(drill_prefix)
    assert second.returncode == 0, second.stdout + second.stderr

    after_second = tree_digest(drill_prefix)
    changed = sorted(
        key for key in after_first | after_second if after_first.get(key) != after_second.get(key)
    )
    assert not changed, f"第二次运行改动了文件：{changed}"
    assert "已存在" in second.stdout or "无变化" in second.stdout
    assert "已写入 /" not in second.stdout and "已安装 /" not in second.stdout

    # ---------- 阶段 3：部署者填过的配置必须被保留 ----------
    env_file = drill_prefix / "etc" / "relay" / "relay.env"
    body = env_file.read_text(encoding="utf-8")
    assert "RELAY_DOMAIN=" in body
    env_file.write_text(
        body.replace("RELAY_DOMAIN=", "RELAY_DOMAIN=relay.hopdrop.cn").replace(
            "RELAY_ICP_LICENSE=", "RELAY_ICP_LICENSE=沪ICP备12345678号-1"
        ),
        encoding="utf-8",
    )
    marker = hashlib.sha256(env_file.read_bytes()).hexdigest()

    third = run_install(drill_prefix)
    assert third.returncode == 0, third.stdout + third.stderr
    assert hashlib.sha256(env_file.read_bytes()).hexdigest() == marker
    assert "RELAY_DOMAIN=relay.hopdrop.cn" in env_file.read_text(encoding="utf-8")
    assert "不覆盖" in third.stdout

    # ---------- 阶段 4：--check 不写任何东西 ----------
    before_check = tree_digest(drill_prefix)
    check = run_install(drill_prefix, "--check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert tree_digest(drill_prefix) == before_check, "--check 不应改动任何文件"
    assert "未做任何改动" in check.stdout


@requires_bash
def test_install_drill_rejects_wrong_layout(tmp_path: Path) -> None:
    """把仓库根整个拷过去（/opt/relay/relay/app）必须被拦住。

    这是最容易犯的一次性错误：`cp -r HopDrop /opt/relay` 看起来很像对的，
    真跑起来报的是 `ModuleNotFoundError: app`，而报错点离原因很远。
    """
    root = tmp_path / "wrong"
    app_dir = root / "opt" / "relay"
    app_dir.mkdir(parents=True)
    shutil.copytree(DEPLOY_DIR, app_dir / "deploy")
    shutil.copytree(RELAY_DIR / "app", app_dir / "relay" / "app")

    result = run_install(root)
    assert result.returncode != 0
    assert "铺错了层级" in result.stdout + result.stderr


def test_install_script_never_enables_firewall() -> None:
    """ufw 只放行、不启用。

    同机有共存服务：`ufw enable` 会按默认策略（deny incoming）挡掉别人的端口，
    而这是运维的决定，不是部署脚本该顺手做的。
    """
    body = read(DEPLOY_DIR / "install.sh")
    # 允许在提示文字里出现 `ufw enable`，但不允许作为被执行的命令。
    executed = re.findall(r"^\s*(?:sysrun\s+)?ufw\s+(?:--force\s+)?enable", body, flags=re.MULTILINE)
    assert not executed, "install.sh 不应启用 ufw"


def test_install_script_requires_root_unless_drilling() -> None:
    body = read(DEPLOY_DIR / "install.sh")
    assert 'if [ "$SYS" = 1 ] && [ "$(id -u)" != "0" ]' in body


def test_install_script_checks_disk_before_deploying() -> None:
    """方案 7.1：磁盘预算的前提是同机有共存服务，部署前必须看一眼。"""
    body = read(DEPLOY_DIR / "install.sh")
    assert "MIN_FREE_MB" in body and "df -Pm" in body


# --------------------------------------------------------------- 健康检查脚本


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """按类属性返回固定状态，用来驱动健康检查脚本的三种分支。"""

    status_code = 200
    payload = b'{"status":"ok"}'

    def do_GET(self) -> None:  # noqa: N802 - http.server 的命名约定
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args: object) -> None:
        pass  # 测试输出保持干净


def _serve(handler_cls: type[http.server.BaseHTTPRequestHandler]) -> tuple[http.server.HTTPServer, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def run_healthcheck(addr: str, state_file: Path) -> subprocess.CompletedProcess:
    return run_bash(
        [
            msys_path(DEPLOY_DIR / "hopdrop-healthcheck.sh"),
            "--addr",
            addr,
            "--state-file",
            msys_path(state_file),
        ]
    )


@requires_bash
@requires_curl
def test_healthcheck_reports_healthy(tmp_path: Path) -> None:
    handler = type("Healthy", (_HealthHandler,), {"status_code": 200, "payload": b'{"status":"ok"}'})
    server, port = _serve(handler)
    try:
        state = tmp_path / "healthcheck.state"
        result = run_healthcheck(f"127.0.0.1:{port}", state)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "fail_count=0" in state.read_text(encoding="utf-8")
        assert "last_status=ok" in state.read_text(encoding="utf-8")
    finally:
        server.shutdown()


@requires_bash
@requires_curl
def test_healthcheck_distinguishes_degraded_from_unreachable(tmp_path: Path) -> None:
    """degraded 与不可达必须给出不同退出码。

    两者的处置完全不同：degraded 要去看 reasons（磁盘/配额），不可达才值得
    考虑重启进程。混成一个"失败"就得先翻日志才能分流。
    """
    body = json.dumps({"status": "degraded", "reasons": ["disk_free_below_reserve"]}).encode()
    handler = type("Degraded", (_HealthHandler,), {"status_code": 503, "payload": body})
    server, port = _serve(handler)
    try:
        state = tmp_path / "degraded.state"
        result = run_healthcheck(f"127.0.0.1:{port}", state)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "degraded" in result.stdout
        # reasons 要带出来，否则还得手工再请求一次才知道原因
        assert "disk_free_below_reserve" in result.stdout
        assert "fail_count=1" in state.read_text(encoding="utf-8")
    finally:
        server.shutdown()

    # 没人监听的端口 → 不可达
    unreachable_state = tmp_path / "unreachable.state"
    result = run_healthcheck("127.0.0.1:1", unreachable_state)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "不可达" in result.stdout


@requires_bash
@requires_curl
def test_healthcheck_resets_failure_counter_after_recovery(tmp_path: Path) -> None:
    """先失败再恢复，计数必须归零。

    不归零的话，一次偶发抖动会永久留在计数里，之后任何一次失败都会立刻
    触发阈值动作。
    """
    state = tmp_path / "recover.state"

    assert run_healthcheck("127.0.0.1:1", state).returncode == 1
    assert "fail_count=1" in state.read_text(encoding="utf-8")

    handler = type("Healthy", (_HealthHandler,), {"status_code": 200})
    server, port = _serve(handler)
    try:
        result = run_healthcheck(f"127.0.0.1:{port}", state)
        assert result.returncode == 0, result.stdout + result.stderr
        content = state.read_text(encoding="utf-8")
        assert "fail_count=0" in content
        assert "last_status=ok" in content
    finally:
        server.shutdown()


# --------------------------------------------------------------- release.sh 打包


@requires_bash
def test_release_pack_only_produces_deployable_archive(tmp_path: Path) -> None:
    """发布包解到 /opt/relay 之后必须正好落位。

    这条是"发布包结构"这个约定的唯一执行者：`app/main.py` 在包的根一层，
    `deploy/` 在根一层，没有多余的 `relay/` 前缀。结构错了要在打包时就发现，
    而不是等服务器上 `ModuleNotFoundError`。
    """
    dist = tmp_path / "dist"
    env = {**os.environ, "RELAY_DIST_DIR": msys_path(dist)}

    result = subprocess.run(
        [
            BASH,
            msys_path(DEPLOY_DIR / "release.sh"),
            "--pack-only",
            "--skip-tests",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    packages = list(dist.glob("hopdrop-*.tar.gz"))
    assert len(packages) == 1, f"应当恰好产出一个包，实际：{packages}"
    package = packages[0]

    # 版本号取自 app/__init__.py
    from app import __version__

    assert package.name.startswith(f"hopdrop-{__version__}-")

    # 校验值文件存在且与包内容一致
    checksum_file = dist / f"{package.name}.sha256"
    assert checksum_file.is_file()
    expected = checksum_file.read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(package.read_bytes()).hexdigest()
    assert expected == actual, "sha256 与包内容不一致"

    # 包内结构
    listing = subprocess.run(
        ["tar", "-tzf", str(package)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    entries = set(listing.stdout.split())

    # 包内成员名有两种前缀：`./app/...`（-C relay .）和 `deploy/...`
    # （-C repo deploy）。归一化后再断言，免得测试锁的是写法而不是结构。
    normalized = {e[2:] if e.startswith("./") else e for e in entries}

    for required in (
        "app/main.py",
        "app/config.py",
        "requirements.txt",
        "relay.env.example",
        "deploy/install.sh",
        "deploy/rollback.sh",
        "deploy/release.sh",
        "deploy/hopdrop-healthcheck.sh",
        "deploy/Caddyfile",
        "deploy/relay.service",
        "MANIFEST.txt",
    ):
        assert required in normalized, f"发布包里缺少 {required}"

    # 这些不该上服务器：测试、本地数据、编译缓存、开发依赖。
    # __pycache__ 尤其要盯住：带上本机字节码之后，改了源码但行为没变，
    # 而排查时没人会想到去看它。
    forbidden = [
        e
        for e in normalized
        if e.startswith("tests/")
        or e.startswith("data/")
        or "__pycache__" in e
        or e.endswith(".pyc")
        or e in {"pytest.ini", "requirements-dev.txt"}
    ]
    assert not forbidden, f"发布包里混进了不该有的文件：{forbidden}"

    assert any(e.endswith(".sql") for e in normalized), "发布包里必须有迁移脚本"

    manifest = dist / "MANIFEST.txt"
    body = manifest.read_text(encoding="utf-8")
    for key in ("version=", "commit=", "built_at=", "rollback="):
        assert key in body, f"MANIFEST 缺少 {key}"


# --------------------------------------------------------------- rollback.sh


def rollback_env(prefix: Path) -> dict[str, str]:
    return {
        **os.environ,
        "RELAY_APP_DIR": f"{prefix}/opt/relay",
        "RELAY_PREV_DIR": f"{prefix}/opt/relay.prev",
        "RELAY_DATA_DIR": f"{prefix}/var/lib/relay",
    }


def build_deployed_tree(prefix: Path, *, with_backup: bool) -> None:
    """手工搭出 rollback.sh 会读的那几个路径。

    不跑 install.sh：这两个测试只验证"读得对不对"，跑一次安装要多花七秒。
    """
    app_dir = prefix / "opt" / "relay"
    (app_dir / "app").mkdir(parents=True)
    (app_dir / "MANIFEST.txt").write_text("version=0.1.0\ncommit=deadbee\n", encoding="utf-8")

    if with_backup:
        prev_dir = prefix / "opt" / "relay.prev"
        (prev_dir / "app").mkdir(parents=True)
        (prev_dir / "MANIFEST.txt").write_text("version=0.0.9\n", encoding="utf-8")

    backup_dir = prefix / "var" / "lib" / "relay" / "backup"
    backup_dir.mkdir(parents=True)
    (backup_dir / "relay-20260925-030000.db").write_bytes(b"SQLite format 3\x00")


@requires_bash
def test_rollback_lists_backups_and_current_version(tmp_path: Path) -> None:
    prefix = tmp_path / "tree"
    build_deployed_tree(prefix, with_backup=True)

    result = run_bash(
        [msys_path(DEPLOY_DIR / "rollback.sh"), "--list"],
        env=rollback_env(msys_path(prefix)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0.1.0" in result.stdout, "应显示当前版本"
    assert "0.0.9" in result.stdout, "应显示备份里的版本"
    assert "relay-20260925-030000.db" in result.stdout, "应列出可用的数据库备份"


@requires_bash
def test_rollback_lists_gracefully_without_backup(tmp_path: Path) -> None:
    """首次部署前跑 --list 是很自然的动作，那时还没有备份，不能报错。"""
    prefix = tmp_path / "tree"
    build_deployed_tree(prefix, with_backup=False)

    result = run_bash(
        [msys_path(DEPLOY_DIR / "rollback.sh"), "--list"],
        env=rollback_env(msys_path(prefix)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "无（" in result.stdout, "没有备份时应给出可读的提示而不是空白"


@requires_bash
def test_rollback_refuses_clearly_without_backup(tmp_path: Path) -> None:
    """没有备份时回滚必须明确失败，而不是"什么都没做但退出 0"。"""
    prefix = tmp_path / "tree"
    build_deployed_tree(prefix, with_backup=False)

    result = run_bash(
        [msys_path(DEPLOY_DIR / "rollback.sh")],
        env=rollback_env(msys_path(prefix)),
    )
    assert result.returncode != 0
    assert "没有可回滚的代码" in result.stdout + result.stderr


@requires_bash
def test_rollback_removes_sqlite_wal_on_database_restore(tmp_path: Path) -> None:
    """回滚数据库时必须删掉 -wal / -shm。

    不删的话 SQLite 会把新 WAL 里未合并的事务重放到旧库上，得到一个两边都
    不像的数据库——而且是静默的。这条断言只能锁住"代码里有这一步"，但那是
    这个动作唯一能被自动检查的地方。
    """
    body = read(DEPLOY_DIR / "rollback.sh")
    assert re.search(r'rm -f "\$DATA_DIR/relay\.db-wal" "\$DATA_DIR/relay\.db-shm"', body), (
        "回滚数据库前必须删除 WAL 与 SHM"
    )
