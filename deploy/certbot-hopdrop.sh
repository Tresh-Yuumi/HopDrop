#!/usr/bin/env bash
#
# 为 HopDrop 域名签一张 Let's Encrypt 证书。
#
# 用法：sudo bash /opt/relay/deploy/certbot-hopdrop.sh <域名> [邮箱]
#
# ---------------------------------------------------------------------------
# 为什么用 `certonly --webroot` 而不是 `certbot --nginx`
#
# `certbot --nginx` 会**改写 nginx 配置**：插入 443 server 块、把 80 改成 301，
# 并打上 `# managed by Certbot` 标记。而 HopDrop 的站点文件是每次发布都由
# install.sh 按仓库版本整体重写的——两者叠在一起的结果是：续期时 certbot 改
# 一遍，下次发布 install.sh 又覆盖回去，配置在两个来源之间反复横跳。
# 这种冲突不会报错，只会在某次"证书怎么没生效"里慢慢显形。
#
# 换成 certonly + webroot 之后职责就干净了：certbot 只负责签证书与续期，
# nginx 配置永远只有一个来源（install.sh）。续期照样是自动的——
# certbot.timer 会读 /etc/letsencrypt/renewal/<域名>.conf 里记下的 webroot。
# ---------------------------------------------------------------------------
#
# 前置条件（脚本会逐条检查）：
#   1. 域名解析已指向这台机器；
#   2. 80 端口能从公网访问（备案未完成时会被云厂商拦掉）；
#   3. install.sh 已生成这个域名的 nginx 站点（带 acme-challenge location）。

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
WEBROOT="/var/www/html"
SITE="/etc/nginx/sites-enabled/hopdrop"

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }
ok() { printf '  [ok]   %s\n' "$*"; }
note() { printf '  [note] %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*" >&2; }
head_line() { printf '\n== %s\n' "$*"; }

[ -n "$DOMAIN" ] || die "用法：bash certbot-hopdrop.sh <域名> [邮箱]"
[ "$(id -u)" = "0" ] || die "需要 root 权限"

head_line "前置检查"

command -v certbot >/dev/null 2>&1 || die "没装 certbot。先执行：apt-get install -y certbot"
ok "certbot $(certbot --version 2>&1 | head -1)"

command -v nginx >/dev/null 2>&1 || die "找不到 nginx 命令。"
ok "nginx 已安装"

if [ ! -e "$SITE" ]; then
	die "找不到 $SITE。
先跑 install.sh 生成站点配置（它要求 /etc/relay/relay.env 里已经填好 RELAY_DOMAIN）：
  bash /opt/relay/deploy/install.sh"
fi
ok "站点配置存在：$SITE"

if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
	ok "证书已存在，不重复签发（Let's Encrypt 对同一域名有速率限制）"
	say "查看：certbot certificates"
	exit 0
fi

head_line "域名解析"

resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)"
if [ -z "$resolved" ]; then
	warn "$DOMAIN 解析不到任何 A 记录。"
	warn "先去域名服务商把这个子域指到这台机器的公网 IP，等解析生效后再跑本脚本。"
	die "解析未生效。"
fi
ok "$DOMAIN → $resolved"
note "本机自身地址：$(ip -brief addr show scope global 2>/dev/null | awk '{print $3}' | paste -sd, -)"
note "两者不一致是正常的：云主机多为 NAT，网卡上看到的是内网地址。"

head_line "webroot 自检"

mkdir -p "$WEBROOT/.well-known/acme-challenge"
probe="$(mktemp "$WEBROOT/.well-known/acme-challenge/hopdrop-probe-XXXXXX")"
printf 'hopdrop-acme-probe\n' >"$probe"
probe_name="$(basename "$probe")"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "Host: $DOMAIN" \
	"http://127.0.0.1/.well-known/acme-challenge/$probe_name" || true)"
rm -f "$probe"

case "$code" in
200) ok "本机能通过 webroot 返回挑战文件" ;;
*)
	warn "本机自检失败（HTTP ${code:-000}）。"
	warn "说明这个域名的请求没有落到 HopDrop 的站点上，或者 acme-challenge 的"
	warn "location 没有生效。先确认："
	warn "  nginx -T | grep 'server_name'"
	warn "  nginx -T | grep -A3 'acme-challenge'"
	die "webroot 自检未通过，不继续——避免白白消耗 ACME 的失败配额。"
	;;
esac

head_line "签发"

args=(certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --agree-tos --keep-until-expiring)
if [ -n "$EMAIL" ]; then
	args+=(--email "$EMAIL")
else
	warn "未提供邮箱：证书到期提醒收不到。有邮箱的话带上第二个参数再跑一次。"
	args+=(--register-unsafely-without-email)
fi

if ! certbot "${args[@]}"; then
	cat >&2 <<'HINT'

签发失败。按出现频率排一下可能的原因：

  1. 备案未完成，80 端口被云厂商拦截。典型症状是"境内连不上、境外能连"，
     ACME 校验会一直超时。阿里云华东节点必须有备案。
  2. 域名解析没生效或指到了别的机器（上面那行解析结果可以核对）。
  3. 云控制台的**安全组**没放行 80。注意这和机器上的 ufw 是两回事：
     本机 80 上确实有 nginx 在监听，不代表公网进得来。
  4. 这台机器上 80 的 default_server 是别的服务（minitalk）。HopDrop 的站点
     有明确 server_name，按 nginx 的匹配顺序不会串味——但如果 server_name
     写错了域名，请求就会掉到 default_server 上去。
HINT
	die "签发失败，上面是排查方向。"
fi

ok "证书已签发：/etc/letsencrypt/live/$DOMAIN/"

head_line "下一步"
cat <<EOF
  1. 重跑 install.sh，站点会从 HTTP 版换成带 443 的版本：
       bash /opt/relay/deploy/install.sh
  2. 确认 /etc/relay/relay.env 里 RELAY_COOKIE_SECURE=1（HTTPS 下的默认值）。
  3. 续期由 certbot.timer 自动完成，无需额外配置。检查：
       systemctl list-timers certbot.timer
       certbot renew --dry-run
EOF
