#!/usr/bin/env bash
#
# HopDrop 部署前只读盘查（方案 17 节「先看再动」）。
#
# 这个脚本只读不写：不装包、不改配置、不重启服务、不动别人的进程。
# 目的是在动手之前先回答四个问题：
#   1. 80 / 443 被谁占着，它是什么反代软件、配置在哪；
#   2. 上面还跑着什么别的东西，能分给 HopDrop 多少磁盘和内存；
#   3. 有没有旧版本 HopDrop 的残留（避免装重）；
#   4. 防火墙现状（我打算只放行、不启用，先看清楚原来是什么样）。
#
# 用法：
#   ssh root@<host> 'bash -s' < deploy/probe.sh
#
set -uo pipefail

sep() { printf '\n\033[1m===== %s =====\033[0m\n' "$1"; }
kv() { printf '%-22s %s\n' "$1" "$2"; }

sep "身份与系统"
kv "用户" "$(id -un)"
kv "主机名" "$(hostname)"
if [ -r /etc/os-release ]; then
	# shellcheck disable=SC1091
	. /etc/os-release
	kv "发行版" "${PRETTY_NAME:-未知}"
fi
kv "内核" "$(uname -r)"
kv "运行时长" "$(uptime -p 2>/dev/null || uptime)"

sep "监听端口一览（tcp/udp，含进程名）"
if command -v ss >/dev/null 2>&1; then
	ss -lntupH 2>/dev/null | awk '{print $1, $5, $7}' | sort -u
else
	netstat -lntup 2>/dev/null | sed 1,2d
fi

sep "★ 80 / 443 到底是谁在监听"
ss -lntpH 2>/dev/null | awk '$4 ~ /:(80|443)$/ {print}' || true
printf '\n--- 进程详情 ---\n'
for port in 80 443; do
	holder="$(ss -lntpH "sport = :$port" 2>/dev/null | head -1 | sed 's/.*users:((//; s/)).*//')"
	if [ -n "$holder" ]; then
		printf ':%s → %s\n' "$port" "$holder"
	else
		printf ':%s → (空闲)\n' "$port"
	fi
done

sep "★ 反代 / Web 服务器软件"
found_any=0
for s in nginx openresty caddy apache2 httpd haproxy traefik frps; do
	path="$(command -v "$s" 2>/dev/null || true)"
	act="$(systemctl is-active "$s" 2>/dev/null || true)"
	ver=""
	[ -n "$path" ] && ver="$("$s" -v 2>&1 | head -1 || true)"
	if [ -n "$path" ] || [ -n "$act" ]; then
		found_any=1
		kv "$s" "路径=${path:-无} 状态=${act:-未安装} ${ver}"
	fi
done
[ "$found_any" = 0 ] && echo "(未发现已知反代软件——80/443 可能是容器或自定义进程)"

sep "★ 容器运行时"
for s in docker podman; do
	if command -v "$s" >/dev/null 2>&1; then
		kv "$s" "$($s ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null | head -10 || echo '无权限或未运行')"
	fi
done

sep "正在运行的服务"
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null \
	| awk '{print "  " $1}'

sep "磁盘"
df -hT / /var /opt /srv 2>/dev/null | grep -vE '^(Filesystem|tmpfs)'
printf '\n--- inode ---\n'
df -i / 2>/dev/null | tail -1

sep "内存与 swap"
free -h
printf '\n--- swap 明细 ---\n'
swapon --show 2>/dev/null || echo "(无 swap)"

sep "★ 旧的 HopDrop / relay 残留"
for p in /opt/relay /opt/relay.prev /etc/relay /var/lib/relay /etc/caddy /etc/nginx /usr/local/bin/hopdrop-backup; do
	if [ -e "$p" ]; then
		printf '  存在: %s\n' "$p"
	else
		printf '  没有: %s\n' "$p"
	fi
done
printf '\n--- 相关 unit ---\n'
systemctl list-unit-files 2>/dev/null | grep -iE 'relay|caddy|nginx|hopdrop' || echo "  (无)"

sep "★ 现有反代的配置（决定 HopDrop 怎么挂进去）"
if [ -d /etc/nginx ]; then
	printf '--- /etc/nginx 结构 ---\n'
	ls -la /etc/nginx/ 2>/dev/null | head -20
	printf '\n--- conf.d ---\n'
	ls -la /etc/nginx/conf.d/ 2>/dev/null
	printf '\n--- sites-enabled ---\n'
	ls -la /etc/nginx/sites-enabled/ 2>/dev/null
	printf '\n--- 现有 server_name 与 listen ---\n'
	grep -rhE '^\s*(listen|server_name)' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | sed 's/^\s*/  /'
fi
if [ -d /etc/caddy ]; then
	printf '\n--- /etc/caddy/Caddyfile ---\n'
	sed 's/^/  /' /etc/caddy/Caddyfile 2>/dev/null | head -60
	printf '\n--- /etc/caddy/conf.d ---\n'
	ls -la /etc/caddy/conf.d/ 2>/dev/null
fi

sep "防火墙现状（只读，不动）"
if command -v ufw >/dev/null 2>&1; then
	printf 'ufw: %s\n' "$(ufw status 2>/dev/null | head -1 || echo '未启用')"
	ufw status numbered 2>/dev/null | head -20 | sed 's/^/  /'
else
	echo "(未装 ufw)"
fi
if command -v firewall-cmd >/dev/null 2>&1; then
	firewall-cmd --state 2>/dev/null || true
fi
printf '\n--- iptables（前 25 条）---\n'
iptables -S 2>/dev/null | head -25 || echo "(无权限或无 iptables)"

sep "已装 Python"
for p in python3 python3.10 python3.11 python3.12 python3.13; do
	command -v "$p" >/dev/null 2>&1 && kv "$p" "$("$p" -V 2>&1)"
done
command -v python3 >/dev/null 2>&1 && kv "venv 模块" "$(python3 -c 'import venv; print("可用")' 2>&1 | tail -1)"

sep "时间与网络"
kv "时间" "$(date -Is)"
kv "时区" "$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo 未知)"
kv "DNS" "$(grep -m1 nameserver /etc/resolv.conf 2>/dev/null | awk '{print $2}')"
printf '\n--- 默认路由 / 公网出口 ---\n'
ip -brief addr show 2>/dev/null | sed 's/^/  /'
ip route get 223.5.5.5 2>/dev/null | sed 's/^/  /'

printf '\n\033[1m===== 盘查结束（未做任何修改） =====\033[0m\n'
