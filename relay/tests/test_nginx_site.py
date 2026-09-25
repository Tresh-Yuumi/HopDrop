"""nginx 共存部署的约束测试。

这一组测试锁的不是"配置长什么样"，而是**几条一旦破了就会静默出事的规则**：

* `X-Forwarded-For` 必须是覆盖式（追加式会让 IP 限流被伪造绕过）；
* 反代的请求体上限必须大于应用上限（否则大文件上传永远看不到应用的错误码）；
* 顺序必须是「写文件 → nginx -t → reload」（顺序反了就是同机所有站点一起挂）；
* 只 reload 不 restart（restart 会瞬断机器上的其他站点）；
* 不装 ufw / fail2ban、不改时区（同机共存，这些都归运维）。

目标机的真实情况见 deploy/RUNBOOK.md：80/443 由既有 nginx 持有，上面还跑着
minitalk（占着 80 的 default_server）、fortranslate、phound、postgres。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_DIR / "deploy"
RELAY_DIR = REPO_DIR / "relay"

BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="需要 bash")

DOMAIN = "relay.test.example"
SITE_REL = "etc/nginx/sites-available/hopdrop"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _effective_lines(body: str) -> list[str]:
    """去掉注释与空行后剩下的内容。

    断言"文件里不含某个字符串"时必须走这里：这些文件的注释里到处都在解释
    "为什么不要用 $proxy_add_x_forwarded_for""为什么不能 restart nginx"，
    直接扫全文会把说明文字当成违规代码。
    """
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def msys_path(path: object) -> str:
    """把 Windows 路径转成 Git Bash 看得懂的形式（非 Windows 上原样返回）。"""
    if shutil.which("cygpath"):
        result = subprocess.run(
            ["cygpath", "-u", str(path)], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return str(path)


def run_install(prefix: Path, *extra: str) -> subprocess.CompletedProcess:
    assert BASH is not None
    return subprocess.run(
        [BASH, msys_path(DEPLOY_DIR / "install.sh"), "--prefix", msys_path(prefix), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def build_tree(root: Path, *, domain: str = DOMAIN) -> None:
    """搭出一棵与服务器同构的目录树，并预置好填了域名的 relay.env。

    预置 relay.env 是为了让一次 install.sh 就走到站点生成分支——否则要先跑一遍
    拿到模板、改域名、再跑一遍，而这个脚本在 Windows 上一次约 7 秒。
    """
    app_dir = root / "opt" / "relay"
    app_dir.mkdir(parents=True)
    for name in ("app", "web", "migrations"):
        shutil.copytree(RELAY_DIR / name, app_dir / name)
    shutil.copy2(RELAY_DIR / "requirements.txt", app_dir / "requirements.txt")
    shutil.copy2(RELAY_DIR / "relay.env.example", app_dir / "relay.env.example")
    shutil.copytree(DEPLOY_DIR, app_dir / "deploy")

    env_dir = root / "etc" / "relay"
    env_dir.mkdir(parents=True)
    (env_dir / "relay.env").write_text(
        read(RELAY_DIR / "relay.env.example").replace("RELAY_DOMAIN=", f"RELAY_DOMAIN={domain}"),
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def site_prefix(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """跑一次 install.sh，拿到真实的站点渲染结果。

    module 作用域是有意的：整个文件里的断言都读同一份产物，跑一次就够。
    """
    root = tmp_path_factory.mktemp("nginx-site")
    build_tree(root)
    result = run_install(root)
    assert result.returncode == 0, result.stdout + result.stderr
    return root


def site_text(prefix: Path) -> str:
    return read(prefix / SITE_REL)


# --------------------------------------------------------------- 站点生成


@requires_bash
def test_site_is_written_and_enabled(site_prefix: Path) -> None:
    assert (site_prefix / SITE_REL).is_file(), "install.sh 没有生成 nginx 站点"
    assert (site_prefix / "etc/nginx/sites-enabled/hopdrop").exists(), "站点没有被启用"


@requires_bash
def test_site_only_serves_the_configured_domain(site_prefix: Path) -> None:
    """有明确 server_name，才能与 80 上那个 default_server 区分开。

    目标机 80 端口的 default_server 是 minitalk。nginx 的匹配顺序是
    精确 server_name 优先于 default_server，所以只要这里写对了域名，
    流量就不会串到别的站点上；写错了就会。
    """
    body = site_text(site_prefix)
    names = [line for line in _effective_lines(body) if line.startswith("server_name ")]
    # 这个 fixture 没有证书，所以只有 80 段；有证书时会多一条 443 段的同名声明
    # （见 test_certificate_turns_the_site_into_https）。
    assert names == [f"server_name {DOMAIN};"], (
        f"站点应只声明自己的域名，实际：{names}"
    )


@requires_bash
def test_site_proxies_to_loopback_upstream(site_prefix: Path) -> None:
    body = site_text(site_prefix)
    assert "proxy_pass http://127.0.0.1:8080;" in body


@requires_bash
def test_site_uses_covering_x_forwarded_for(site_prefix: Path) -> None:
    r"""**这是这一组里最重要的一条。**

    应用侧的 client_ip() 取 X-Forwarded-For 的**第一个**值当作来源 IP，
    用来执行方案 10 的单 IP 并发上限（默认 10）。如果这里写成
    `$proxy_add_x_forwarded_for`（nginx 文档里最常见的写法），客户端自己
    带的伪造值会排在真实地址前面——任何人加一个请求头就能绕过连接上限。

    断言同时覆盖"必须用 $remote_addr"和"不得出现追加式"两面。
    """
    lines = _effective_lines(site_text(site_prefix))
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in lines
    assert not any("proxy_add_x_forwarded_for" in line for line in lines), (
        "追加式写 XFF 会让客户端伪造的来源 IP 排在首位，IP 限额随之失效"
    )


@requires_bash
def test_site_keeps_host_without_port(site_prefix: Path) -> None:
    """Host 带端口会让应用的 same_origin() 判定失败。

    same_origin() 是拿 Origin 的 netloc 与 Host 头做字符串相等比较。
    浏览器发的是 `https://relay.test.example`（无端口），若这里传
    `$http_host` 在非标准端口下会带上 `:8443`，两者不等——症状是
    WebSocket 全被拒，而 HTTP 请求看起来完全正常。
    """
    body = site_text(site_prefix)
    assert "proxy_set_header Host $host;" in body
    assert "proxy_set_header Host $http_host;" not in body


@requires_bash
def test_site_body_limit_exceeds_application_limit(site_prefix: Path) -> None:
    """反代的请求体上限必须大于应用的单文件上限。

    反过来会让应用永远收不到"文件过大"这个自己的错误码，用户看到的是
    nginx 的 413 纯文本。nginx 全局没设 client_max_body_size（默认 1 MiB），
    所以这一条必须由站点自己兜住。
    """
    from app.config import MAX_FILE_BYTES_HARD_LIMIT

    match = re.search(r"client_max_body_size\s+(\d+)m", site_text(site_prefix))
    assert match, "站点里找不到 client_max_body_size"

    limit = int(match.group(1)) * 1024 * 1024
    assert limit > MAX_FILE_BYTES_HARD_LIMIT, (
        f"反代上限 {limit} 不大于应用上限 {MAX_FILE_BYTES_HARD_LIMIT}"
    )


@requires_bash
def test_site_has_dedicated_websocket_location(site_prefix: Path) -> None:
    """WebSocket 要单独一个 location，别把普通请求也标成 upgrade。

    路径与 relay/app/api/realtime.py 里的 `@router.websocket("/ws")` 对应，
    改了一处忘了另一处，症状是页面连不上、服务端无日志。
    """
    body = site_text(site_prefix)
    assert "location = /ws {" in body
    assert "proxy_set_header Upgrade $http_upgrade;" in body
    assert 'proxy_set_header Connection "upgrade";' in body


@requires_bash
def test_site_read_timeout_outlives_websocket_keepalive(site_prefix: Path) -> None:
    """反代的读超时必须明显长于应用的空闲超时。

    客户端每 25 秒 ping 一次，应用侧 60 秒收不到活动就关连接
    （RELAY_WS_IDLE_TIMEOUT_SEC 默认 60）。nginx 默认的 60s 正好卡在这个
    边界上，一轮网络抖动就可能切断——表现是页面"偶尔自己刷新"，
    而服务端日志里什么都没有。
    """
    from app.config import load_config

    idle = load_config({}).ws_idle_timeout_sec
    body = site_text(site_prefix)

    timeouts = [int(v) for v in re.findall(r"proxy_read_timeout (\d+)s", body)]
    assert timeouts, "站点里找不到 proxy_read_timeout"
    assert min(timeouts) > idle * 2, (
        f"读超时 {min(timeouts)}s 相对应用空闲超时 {idle}s 余量不足"
    )


@requires_bash
def test_site_answers_acme_challenge_before_redirecting(site_prefix: Path) -> None:
    """certbot 用 webroot 签证书，HTTP-01 挑战必须由 80 段自己接住。

    用 `^~` 前缀匹配压过 location /，否则挑战请求会被反代或跳转吃掉，
    证书就永远签不下来。
    """
    body = site_text(site_prefix)
    assert "location ^~ /.well-known/acme-challenge/ {" in body
    assert "root /var/www/html;" in body


@requires_bash
def test_site_separates_its_access_log(site_prefix: Path) -> None:
    """日志单独落文件，不与既有站点混在全局 access.log 里。

    混在一起之后，"这台机器上谁在被扫"这类问题就没法回答了。
    """
    body = site_text(site_prefix)
    assert "access_log /var/log/nginx/hopdrop.access.log;" in body
    assert "error_log  /var/log/nginx/hopdrop.error.log;" in body


# --------------------------------------------------------------- HTTP / TLS 两态


@requires_bash
def test_site_is_http_only_until_certificate_exists(site_prefix: Path) -> None:
    """证书不存在时只生成 80 段——引用一个不存在的证书会让 nginx -t 失败，
    进而让整个 reload 流程中断，连带影响同机其他站点。"""
    body = site_text(site_prefix)
    assert body.count("listen 80;") == 1
    assert "listen 443" not in body
    assert "ssl_certificate" not in body


@requires_bash
def test_certificate_turns_the_site_into_https(tmp_path: Path) -> None:
    """证书一出现，站点就切换成"80 跳转 + 443 反代"。

    用一张空文件冒充证书即可：本测试验证的是渲染分支，不是证书内容
    （演练模式本来就会跳过 nginx -t）。
    """
    build_tree(tmp_path)
    assert run_install(tmp_path).returncode == 0
    assert "listen 443" not in site_text(tmp_path)

    cert = tmp_path / "etc/letsencrypt/live" / DOMAIN / "fullchain.pem"
    cert.parent.mkdir(parents=True)
    cert.touch()

    result = run_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    body = site_text(tmp_path)
    assert "listen 443 ssl;" in body
    assert f"ssl_certificate     /etc/letsencrypt/live/{DOMAIN}/fullchain.pem;" in body
    assert f"ssl_certificate_key /etc/letsencrypt/live/{DOMAIN}/privkey.pem;" in body
    # 80 段此时只负责跳转，但仍要接住 ACME 挑战（续期走的就是 80）。
    assert "return 301 https://$host$request_uri;" in body
    assert "location ^~ /.well-known/acme-challenge/ {" in body
    # 443 段同样要有那套 WebSocket 与来源 IP 的配置，不能只在 80 段有。
    assert body.count("location = /ws {") == 1
    assert body.count("proxy_set_header X-Forwarded-For $remote_addr;") == 2

    # HSTS：Caddy 在 HTTPS 下自动加，nginx 不会。切过来之后这一条最容易丢——
    # 域名照常能开、证书照样有效，只是"浏览器从此只用 HTTPS"静默消失。
    effective = _effective_lines(body)
    hsts = [line for line in effective if "Strict-Transport-Security" in line]
    assert hsts, "HTTPS 段缺少 HSTS 头"
    assert "max-age=" in hsts[0]

    # 但绝不能带 includeSubDomains：那会把 devilsarchive.cn 下其它子域
    # （translate / phound / minitalk）一起拖进强制 HTTPS，而它们不归 HopDrop 管。
    assert not any("includeSubDomains" in line for line in effective), (
        "HSTS 带了 includeSubDomains，会波及同机其它站点"
    )
    assert not any("preload" in line for line in effective), "HSTS 不应带 preload"

    # certbot 生成的通用 TLS 参数：协议版本与 DH 参数都从这里来。
    assert "ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;" in effective


@requires_bash
def test_certbot_script_never_edits_nginx_config() -> None:
    """用 certonly 而不是 certbot --nginx。

    `certbot --nginx` 会改写 nginx 配置并打上 `# managed by Certbot`，
    而站点文件是每次发布都由 install.sh 重写的——两个来源互相覆盖，
    配置会在续期与发布之间反复横跳，且不会报错。
    """
    body = read(DEPLOY_DIR / "certbot-hopdrop.sh")
    assert "certonly" in body
    assert "--webroot" in body
    assert "--nginx" not in "\n".join(_effective_lines(body))


@requires_bash
def test_certbot_script_verifies_webroot_before_requesting() -> None:
    """先本地自检再请求证书：Let's Encrypt 对失败次数有限额，
    盲目重试会把这个域名临时锁住。"""
    body = read(DEPLOY_DIR / "certbot-hopdrop.sh")
    idx_selfcheck = body.index("webroot 自检")
    idx_certbot = body.index('if ! certbot "${args[@]}"')
    assert idx_selfcheck < idx_certbot


# --------------------------------------------------------------- 共存安全约束


def test_install_reloads_nginx_but_never_restarts_it() -> None:
    """reload 平滑（既有连接不断），restart 会瞬断同机所有站点。

    这一条是"不影响其他服务"最直接的落点：这台机器上还有 translate、
    phound、minitalk 三个站点在跑。
    """
    body = "\n".join(_effective_lines(read(DEPLOY_DIR / "install.sh")))
    assert "systemctl reload nginx" in body
    assert "restart nginx" not in body, "绝不能重启 nginx"
    assert "stop nginx" not in body


def test_install_validates_nginx_config_before_reloading() -> None:
    """顺序必须是「写文件 → nginx -t → reload」。

    反过来的话，一份语法错误的配置会在 reload 那一刻把所有站点一起打挂。
    这里断言校验在 reload 之前，并且校验失败时走 die（而不是继续 reload）。
    """
    body = read(DEPLOY_DIR / "install.sh")

    idx_validate = body.index("if nginx -t >/dev/null 2>&1; then")
    idx_reload = body.index("sysrun systemctl reload nginx")
    assert idx_validate < idx_reload, "nginx -t 必须发生在 reload 之前"

    assert "nginx 配置校验失败" in body
    # 失败分支里必须回滚，且明确说明没有 reload。
    assert "未执行 reload" in body


def test_install_rolls_back_when_validation_fails() -> None:
    """校验失败要把文件恢复原样，不能留一份坏配置在磁盘上。

    留着的话，下一次任何原因触发的 nginx reload（比如 certbot 续期）都会
    把坏配置读进去——那时已经离本次部署很远了，很难联想到一起。
    """
    body = read(DEPLOY_DIR / "install.sh")
    assert "prune_nginx_backups" in body
    assert ".before-hopdrop-" in body
    assert "cp -p \"$backup\" \"$NGINX_SITE\"" in body


def test_install_never_installs_ufw_or_fail2ban() -> None:
    """同机共存时这两个包是风险源，不该由应用部署脚本带上来。

    ufw 装了就有人 enable，而 enable 按默认策略 deny incoming 会把共存服务
    的端口一起挡掉；fail2ban 装上就启用 sshd jail，可能把部署者自己 ban 掉。
    """
    body = read(DEPLOY_DIR / "install.sh")
    match = re.search(r'^\s*local pkgs="([^"]*)"', body, flags=re.MULTILINE)
    assert match, "找不到依赖列表"

    pkgs = match.group(1).split()
    assert "ufw" not in pkgs
    assert "fail2ban" not in pkgs
    # 必备的还在
    assert "python3-venv" in pkgs


def test_install_never_changes_the_timezone() -> None:
    """改时区会连带改变同机其他服务的日志时间戳，属于运维决定。

    脚本里**出现**这个命令字符串是允许的——它出现在给部署者的提示里
    （"确实需要就手工执行：timedatectl set-timezone Asia/Shanghai"）。
    所以断言必须盯"有没有真的执行"，而不是"有没有提到"。
    """
    body = read(DEPLOY_DIR / "install.sh")
    executed = re.findall(
        r"^\s*(?:sysrun\s+)?timedatectl\s+set-timezone", body, flags=re.MULTILINE
    )
    assert not executed, "install.sh 不应改时区"
    # 但必须把这件事告诉部署者，否则"脚本没管"和"脚本忘了"看起来一样。
    assert "set-timezone" in body


def test_install_never_enables_the_firewall() -> None:
    body = read(DEPLOY_DIR / "install.sh")
    executed = re.findall(
        r"^\s*(?:sysrun\s+)?ufw\s+(?:--force\s+)?enable", body, flags=re.MULTILINE
    )
    assert not executed, "install.sh 不应启用 ufw"


def test_install_supports_explicit_proxy_modes() -> None:
    """既支持自动探测，也允许显式覆盖（目标机换了反代时不用改脚本）。"""
    body = read(DEPLOY_DIR / "install.sh")
    assert "auto | nginx | caddy | none" in body
    assert "detect_proxy" in body


@requires_bash
def test_install_skips_site_generation_without_domain(tmp_path: Path) -> None:
    """没有域名就不生成站点，并给出可执行的提示。

    这台机器 80 端口的 default_server 是 minitalk，随便拿个 server_name
    顶上会让流量串到别人站点去——宁可什么都不配。
    """
    build_tree(tmp_path, domain="")

    result = run_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / SITE_REL).exists(), "没有域名时不该生成站点"
    output = result.stdout + result.stderr
    assert "RELAY_DOMAIN" in output


def test_release_regenerates_site_after_writing_domain() -> None:
    """首次部署时，nginx 站点是在写完域名**之后**才生成的。

    install.sh 按 RELAY_DOMAIN 渲染站点，而 relay.env 是它自己第一次跑时从模板
    生成的（那一项为空，于是跳过站点生成）。release.sh 若在写完域名后直接收工，
    vhost 就永远不存在——80 端口的请求会落到这台机器的 default_server
    （minitalk）上，症状是"用自己的域名访问，看到的却是别人的站点"。
    """
    body = read(DEPLOY_DIR / "release.sh")
    idx_domain_write = body.index("RELAY_DOMAIN=%s 已写入")
    tail = body[idx_domain_write:]
    assert "install.sh" in tail, (
        "写完域名后没有重跑 install.sh：站点不会被生成"
    )


# --------------------------------------------------------------- relay.service


def test_relay_unit_caps_memory() -> None:
    """内存护栏：目标机可用内存只有三百多 MiB。

    没有上限时，HopDrop 一旦内存泄漏，内核会在整机范围内挑进程杀——
    被挑中的可能是 minitalk 或 postgres。设了 cgroup 上限之后，
    被杀的只可能是 HopDrop 自己（Restart=on-failure 会把它拉起来）。
    """
    unit = read(DEPLOY_DIR / "relay.service")
    assert re.search(r"^MemoryHigh=\d+[MG]$", unit, flags=re.MULTILINE)
    assert re.search(r"^MemoryMax=\d+[MG]$", unit, flags=re.MULTILINE)


def test_relay_unit_trusts_forwarded_headers_from_loopback_only() -> None:
    """信任 X-Forwarded-*，但只信来自回环（= 只信本机的 nginx）。"""
    unit = read(DEPLOY_DIR / "relay.service")
    assert "--proxy-headers" in unit
    assert "--forwarded-allow-ips 127.0.0.1" in unit


def test_relay_unit_puts_upload_spill_on_disk_not_tmpfs() -> None:
    """PrivateTmp 给的 /tmp 是 tmpfs——那是内存。

    一次 20 MiB 的上传会实打实吃掉 20 MiB 内存，而 TMPDIR 指到数据盘就没有
    这个问题（那块盘还剩 31 GiB）。
    """
    unit = read(DEPLOY_DIR / "relay.service")
    assert "TMPDIR=/var/lib/relay/tmp" in unit


def test_install_creates_the_tmpdir_the_unit_points_at() -> None:
    """unit 里的 TMPDIR 必须由 install.sh 建出来，否则 UploadFile 会退回到
    /tmp，白设一场。"""
    body = read(DEPLOY_DIR / "install.sh")
    assert "$STATE_DIR/tmp" in body
