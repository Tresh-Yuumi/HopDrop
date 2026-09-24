#!/usr/bin/env bash
#
# HopDrop 服务器初始化 / 修复（方案 v1.4 第 14 章）。
#
# 职责单一：把一台 Ubuntu 机器配置成"能跑 HopDrop"。**不负责上传代码**——
# 那是 deploy/release.sh 的事（在开发机上跑）。这样分工是因为代码每次发布都在
# 变、系统配置几乎不变；混在一起的话，每次发布都要重新评估会不会动到系统状态。
#
# 幂等是硬要求，不是加分项：首次部署和之后每次发布都会调用它。
# 已存在的 /etc/relay/relay.env **一律不覆盖**——里面有部署者填的域名与备案
# 信息，覆盖一次就可能把主人链接和备案号一起抹掉。脚本只报告模板里新增的键。
#
# 用法：
#   sudo bash deploy/install.sh             # 初始化或修复
#   sudo bash deploy/install.sh --check     # 只报告差异，不做任何改动
#   bash deploy/install.sh --no-system --prefix /tmp/drill
#                                           # 本机演练：跳过 apt/ufw/systemd/
#                                           #   useradd，把绝对路径挂到 --prefix 下
#
# 演练模式是为"在没有 Linux 机器的情况下验证这个脚本本身"而存在的。它跑的是
# 真实的文件生成、权限与幂等逻辑，只把需要真系统的部分让开。

set -euo pipefail

# ---------------------------------------------------------------- 常量与参数

# 可被 --prefix / --no-system 覆盖，所以不用 readonly。
PREFIX="/"
SYS=1
CHECK=0

APP_USER="relay"
APP_GROUP="relay"
PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# 磁盘预算（方案 7.1）：小于这个值就拒绝部署，别把共存服务挤死。
MIN_FREE_MB=3072

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_DIR=""
STATE_DIR=""
ENV_DIR=""
ENV_FILE=""
UNIT_FILE=""
HEALTH_UNIT=""
HEALTH_TIMER=""
BACKUP_BIN=""
HEALTH_BIN=""
CRON_FILE=""
CADDYFILE=""
CADDY_DROPIN=""

usage() {
	cat <<'EOF'
用法：sudo bash deploy/install.sh [选项]

  --check             只报告将要做的改动，不写任何文件、不动任何服务
  --prefix DIR        把 /opt/relay、/var/lib/relay 等绝对路径挂到 DIR 下（演练用）
  --no-system         跳过 apt / ufw / systemctl / useradd 等需要真实系统与 root 的步骤
  -h, --help          显示本帮助

环境变量：
  PIP_INDEX_URL       pip 源，默认清华镜像
EOF
}

die() {
	printf '错误：%s\n' "$*" >&2
	exit 1
}

say() { printf '  %s\n' "$*"; }
ok() { printf '  [ok]   %s\n' "$*"; }
note() { printf '  [note] %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*" >&2; }
head_line() { printf '\n== %s\n' "$*"; }

parse_args() {
	while [ $# -gt 0 ]; do
		case "$1" in
		--check) CHECK=1 ;;
		--no-system) SYS=0 ;;
		--prefix)
			shift
			[ $# -gt 0 ] || die "--prefix 需要一个目录参数"
			PREFIX="$1"
			;;
		--prefix=*) PREFIX="${1#--prefix=}" ;;
		-h | --help)
			usage
			exit 0
			;;
		*) die "未知参数：$1（--help 看用法）" ;;
		esac
		shift
	done

	if [ "$PREFIX" != "/" ]; then
		SYS=0
	fi
}

setup_paths() {
	# ${PREFIX%/} 是为了 PREFIX=/ 时不产出 //opt/relay 这种路径。
	# 这类路径本身能用，但会出现在日志和 systemd 的报错里，读起来像出了问题。
	local p="${PREFIX%/}"
	APP_DIR="$p/opt/relay"
	STATE_DIR="$p/var/lib/relay"
	ENV_DIR="$p/etc/relay"
	ENV_FILE="$ENV_DIR/relay.env"
	UNIT_FILE="$p/etc/systemd/system/relay.service"
	HEALTH_UNIT="$p/etc/systemd/system/hopdrop-healthcheck.service"
	HEALTH_TIMER="$p/etc/systemd/system/hopdrop-healthcheck.timer"
	BACKUP_BIN="$p/usr/local/bin/hopdrop-backup"
	HEALTH_BIN="$p/usr/local/bin/hopdrop-healthcheck"
	CRON_FILE="$p/etc/cron.d/hopdrop-backup"
	CADDYFILE="$p/etc/caddy/Caddyfile"
	CADDY_DROPIN="$p/etc/systemd/system/caddy.service.d/10-hopdrop.conf"
}

# ---------------------------------------------------------------- 基础动作

# 需要真实系统才能做的事，统一走这里；演练模式下只打印。
sysrun() {
	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：$*"
		return 0
	fi
	if [ "$CHECK" = 1 ]; then
		note "计划执行：$*"
		return 0
	fi
	"$@"
}

# 写入文件的统一入口：--check 时只报告，内容没变时不动它。
#
# "内容没变就不写"不只是为了让日志好看：这些文件里有 systemd 单元，
# 每次触碰都会让下一步的 daemon-reload 真的重载，而重载之后跟不跟一次
# restart 是很难一眼看出来的（见 step_caddy 的说明）。
write_file() {
	local path="$1" mode="$2" owner="$3" group="$4"
	shift 4

	local tmp
	tmp="$(mktemp)"
	cat >"$tmp"

	if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
		rm -f "$tmp"
		ok "无变化 $path"
		return 0
	fi

	if [ "$CHECK" = 1 ]; then
		rm -f "$tmp"
		note "计划写入 $path（$mode $owner:$group）"
		return 0
	fi

	mkdir -p "$(dirname "$path")"
	# 用 mv 做原子替换：直接 `cat > path` 会在写入过程中留下一个半截文件，
	# 万一正好在那一刻断电或被 kill，systemd 下次读到的就是坏单元。
	mv "$tmp" "$path"
	chmod "$mode" "$path"
	if [ "$SYS" = 1 ]; then
		chown "$owner:$group" "$path"
	fi
	ok "已写入 $path"
}

# 复制仓库里的文件过去，并统一属主与权限。同样带"内容没变就不动"。
copy_in() {
	local src="$1" dest="$2" mode="$3"
	if [ ! -f "$src" ]; then
		die "找不到源文件 $src（deploy/ 目录是否完整？）"
	fi
	if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
		ok "无变化 $dest"
		return 0
	fi
	if [ "$CHECK" = 1 ]; then
		note "计划复制 $src → $dest（$mode）"
		return 0
	fi
	mkdir -p "$(dirname "$dest")"
	install -m "$mode" "$src" "$dest"
	if [ "$SYS" = 1 ]; then
		chown root:root "$dest"
	fi
	ok "已安装 $dest"
}

# ---------------------------------------------------------------- 各步骤

step_preflight() {
	head_line "预检"

	if [ "$SYS" = 1 ] && [ "$(id -u)" != "0" ]; then
		die "需要 root 权限运行（或用 --no-system --prefix 做本机演练）。"
	fi

	case "$(uname -s)" in
	Linux) ;;
	*) note "当前系统是 $(uname -s)，不是 Linux。系统级步骤应当只在目标服务器上执行。" ;;
	esac

	if [ -f "$APP_DIR/app/main.py" ]; then
		ok "代码已就位：$APP_DIR"
	else
		warn "在 $APP_DIR 找不到 app/main.py"
		warn "系统配置会照常完成，但服务在代码就位前起不来。"
		warn "代码由开发机执行 deploy/release.sh 推送，它会在推送后自动调用本脚本。"
	fi

	# 注意判据是 relay/ 的内容直接铺在 APP_DIR 下，而不是 APP_DIR/relay/。
	# deploy/relay.service 里写的是 WorkingDirectory=/opt/relay 加
	# `-m uvicorn app.main:app`，也就是要求 /opt/relay/app/ 存在。
	# 仓库根目录下的 relay/ 与 deploy/ 是同级目录，铺的时候要把 relay/ 的
	# **内容**铺到 /opt/relay/，deploy/ 则整体放到 /opt/relay/deploy/。
	if [ -d "$APP_DIR/relay" ] && [ ! -d "$APP_DIR/app" ]; then
		die "$APP_DIR 下出现了 relay/ 子目录，说明代码铺错了层级。
正确做法：把仓库 relay/ 目录的内容铺到 $APP_DIR（例如 tar -C relay .），
这样才有 $APP_DIR/app/main.py；deploy/ 放到 $APP_DIR/deploy/。"
	fi

	if [ -d "$STATE_DIR" ]; then
		local free_mb
		free_mb="$(df -Pm "$STATE_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
		if [ -n "${free_mb}" ] && [ "${free_mb}" -lt "$MIN_FREE_MB" ]; then
			warn "$STATE_DIR 所在分区只剩 ${free_mb} MiB，低于部署要求（${MIN_FREE_MB} MiB）。"
			warn "方案 7.1 的磁盘预算前提是同机有共存服务，请先清理再继续。"
		else
			ok "磁盘剩余 ${free_mb:-未知} MiB（下限 ${MIN_FREE_MB} MiB）"
		fi
	fi
}

step_packages() {
	head_line "系统包"

	local pkgs="python3 python3-venv python3-pip curl ufw fail2ban sqlite3 git ca-certificates gnupg"
	local missing=""
	local p
	for p in $pkgs; do
		if ! command -v "$p" >/dev/null 2>&1 && ! dpkg -s "$p" >/dev/null 2>&1; then
			missing="$missing $p"
		fi
	done

	if [ -z "$missing" ]; then
		ok "依赖包齐全"
		return
	fi

	say "需要安装：$missing"
	if [ "$CHECK" = 1 ]; then
		note "计划执行 apt-get install$missing"
		return
	fi
	sysrun apt-get update -qq
	# --no-install-recommends：磁盘只有 6 GB 且要留共存服务的地方。
	# DEBIAN_FRONTEND 是为了让 tzdata 这类包不要在无人值守时停下来等输入。
	# shellcheck disable=SC2086
	sysrun env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
	# 方案 14.1：磁盘紧张，不留 apt 缓存。
	sysrun apt-get clean
}

step_timezone_swap() {
	head_line "时区与 swap"

	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：时区与 swap"
		return
	fi

	if command -v timedatectl >/dev/null 2>&1; then
		if [ "$(timedatectl show -p Timezone --value 2>/dev/null)" = "Asia/Shanghai" ]; then
			ok "时区已是 Asia/Shanghai"
		else
			sysrun timedatectl set-timezone Asia/Shanghai
		fi
	fi

	# 2 GiB 内存跑 uvicorn + SQLite 够用，swap 是给系统留的余量。
	if swapon --show=NAME --noheadings 2>/dev/null | grep -q .; then
		ok "已有 swap"
		return
	fi
	if [ -f /swapfile ]; then
		note "/swapfile 已存在但未启用，尝试启用"
		sysrun swapon /swapfile || warn "启用 /swapfile 失败，请手工检查"
		return
	fi

	say "创建 1 GiB /swapfile"
	sysrun fallocate -l 1G /swapfile || sysrun dd if=/dev/zero of=/swapfile bs=1M count=1024
	sysrun chmod 600 /swapfile
	sysrun mkswap /swapfile
	sysrun swapon /swapfile
	if ! grep -q '^/swapfile' /etc/fstab 2>/dev/null; then
		sysrun sh -c "echo '/swapfile none swap sw 0 0' >> /etc/fstab"
	fi
	ok "swap 已启用"
}

step_user() {
	head_line "服务账号"

	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：useradd $APP_USER"
		return
	fi

	if id -u "$APP_USER" >/dev/null 2>&1; then
		ok "用户 $APP_USER 已存在"
		return
	fi

	# --home-dir 就是 APP_DIR：代码放家目录，venv 也放这里（方案 14.1）。
	# shell 用 nologin——服务账号不需要交互式登录。
	sysrun useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
	ok "已创建用户 $APP_USER"
}

step_dirs() {
	head_line "数据目录"

	local d
	local dirs="$STATE_DIR $STATE_DIR/files $STATE_DIR/thumbs $STATE_DIR/backup $APP_DIR $ENV_DIR"

	for d in $dirs; do
		if [ -d "$d" ]; then
			ok "已存在 $d"
			continue
		fi
		if [ "$CHECK" = 1 ]; then
			note "计划创建 $d"
			continue
		fi
		# 用 mkdir + chmod 而不是 `install -d -m 0700`：后者在 MSYS/Git Bash
		# 下会以 "cannot change permissions" 失败（本机演练跑不起来），
		# 而 mkdir 配合脚本开头的 umask 077 在 Linux 上得到同样的结果。
		mkdir -p "$d"
		if ! chmod 0700 "$d" 2>/dev/null; then
			if [ "$SYS" = 1 ]; then
				die "无法把 $d 的权限设为 0700"
			fi
			note "演练环境无法设置目录权限，Linux 上会设为 0700"
		fi
		if [ "$SYS" = 1 ]; then
			chown "$APP_USER:$APP_GROUP" "$d"
		fi
		ok "已创建 $d"
	done

	# 数据目录必须只有服务账号能进：里面有房间数据库和用户上传的文件。
	if [ "$CHECK" = 0 ] && [ "$SYS" = 1 ]; then
		chmod 0700 "$STATE_DIR" "$STATE_DIR"/files "$STATE_DIR"/thumbs "$STATE_DIR"/backup 2>/dev/null || true
	fi
}

step_venv() {
	head_line "Python 环境"

	if [ ! -f "$APP_DIR/requirements.txt" ]; then
		warn "$APP_DIR/requirements.txt 不存在，跳过 venv 与依赖安装。"
		warn "先发布代码（开发机执行 deploy/release.sh），它会再调用一次本脚本。"
		return
	fi

	if [ -x "$APP_DIR/venv/bin/python" ]; then
		ok "venv 已存在"
	else
		say "创建 venv：$APP_DIR/venv"
		if [ "$CHECK" = 1 ]; then
			note "计划执行 python3 -m venv $APP_DIR/venv"
		else
			mkdir -p "$APP_DIR"
			sysrun python3 -m venv "$APP_DIR/venv"
			if [ "$SYS" = 1 ]; then
				chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/venv"
			fi
			ok "venv 已创建"
		fi
	fi

	if [ ! -x "$APP_DIR/venv/bin/pip" ]; then
		note "venv 尚未可用，依赖安装留到下次运行"
		return
	fi

	# Ubuntu 26.04 受 PEP 668 约束，系统 Python 不能直接 pip install（方案 14.2）。
	# --no-cache-dir 是刻意的：磁盘紧张，不需要留安装缓存。
	if [ "$CHECK" = 1 ]; then
		note "计划执行 pip install -r $APP_DIR/requirements.txt"
		return
	fi
	say "安装依赖（源：$PIP_INDEX）"
	sysrun "$APP_DIR/venv/bin/pip" install --no-cache-dir --disable-pip-version-check \
		--index-url "$PIP_INDEX" -r "$APP_DIR/requirements.txt"
	ok "依赖已安装"
}

# 从模板里取"已启用"的键名（行首大写 + =，不是注释行）。
env_keys_of() {
	awk -F= '/^[A-Z_][A-Z0-9_]*=/ {print $1}' "$1" | sort -u
}

step_env_file() {
	head_line "环境变量文件"

	local template="$APP_DIR/relay.env.example"
	if [ ! -f "$template" ]; then
		warn "找不到模板 $template，跳过。"
		return
	fi

	if [ ! -f "$ENV_FILE" ]; then
		say "从模板生成 $ENV_FILE"
		copy_in "$template" "$ENV_FILE" 0600
		note "按需填这三项：RELAY_DOMAIN（域名）、RELAY_ICP_LICENSE（备案号）、RELAY_CONTACT（举报联系方式）"
		note "改完执行：systemctl restart relay"
		return
	fi

	ok "$ENV_FILE 已存在，不覆盖"
	# 发布新版本时模板可能新增了配置项。缺的项在旧文件里没有，服务就会
	# 用代码里的默认值跑——大部分时候没问题，但像 RELAY_DOMAIN 这种没有
	# 合理默认值的项必须让部署者知道。
	local missing
	missing="$(comm -23 <(env_keys_of "$template") <(env_keys_of "$ENV_FILE") || true)"
	if [ -n "$missing" ]; then
		warn "模板里有而 $ENV_FILE 里没有的配置项："
		printf '         %s\n' $missing >&2
		warn "它们将使用代码内默认值。必要时请手工补进 $ENV_FILE。"
	fi

	# 生产必须开 Secure（方案 12）。关掉是本地开发的权宜，线上关掉等于
	# 把会话 Cookie 暴露在明文 HTTP 上。
	if grep -qE '^RELAY_COOKIE_SECURE=(0|false|no|off)$' "$ENV_FILE" 2>/dev/null; then
		warn "RELAY_COOKIE_SECURE 被设为关闭：会话 Cookie 不再要求 HTTPS。"
		warn "这只应在本地 http://127.0.0.1 调试时使用，生产必须改回 1。"
	fi

	# ICP 备案号是监管信息（方案 11.1 / 第 13 节）。方案第 17 节标的是"待填"，
	# 所以这里只提醒，不阻断部署——首页会少渲染那一行，而不是渲染一个假号。
	if ! grep -qE '^RELAY_ICP_LICENSE=.+' "$ENV_FILE" 2>/dev/null; then
		warn "RELAY_ICP_LICENSE 未填写：首页不会展示备案信息。上线前必须补上。"
	fi
	if ! grep -qE '^RELAY_CONTACT=.+' "$ENV_FILE" 2>/dev/null; then
		warn "RELAY_CONTACT 未填写：首页不会展示违规内容举报联系方式（方案 13）。"
	fi
}

step_relay_unit() {
	head_line "relay systemd unit"

	local src="$SRC_DIR/relay.service"
	[ -f "$src" ] || {
		warn "找不到 $src，跳过"
		return
	}

	if [ -f "$UNIT_FILE" ] && cmp -s "$src" "$UNIT_FILE"; then
		ok "unit 无变化"
		return
	fi

	copy_in "$src" "$UNIT_FILE" 0644
	# 只在单元真的变了才 reload。无条件 reload 的代价不只是那点开销：
	# 它会让后续"服务为什么重启了"这类问题多一个嫌疑点。
	if [ "$CHECK" = 0 ] && [ "$SYS" = 1 ]; then
		sysrun systemctl daemon-reload
	fi
}

step_caddy() {
	head_line "Caddy"

	if ! command -v caddy >/dev/null 2>&1; then
		say "安装 Caddy（官方 APT 源）"
		if [ "$CHECK" = 1 ]; then
			note "计划执行 Caddy 官方源安装"
		else
			sysrun sh -c "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
			sysrun sh -c "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null"
			sysrun apt-get update -qq
			sysrun env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends caddy
			sysrun apt-get clean
		fi
	else
		ok "Caddy 已安装（$(caddy version 2>/dev/null | head -1)）"
	fi

	# 让 Caddy 能读到 RELAY_DOMAIN。Caddyfile 里写的是 {$RELAY_DOMAIN::80}，
	# 意思是"取环境变量，没有就用 :80（纯 HTTP）"。把 relay.env 作为
	# EnvironmentFile 注入，域名就只需要维护一处。
	#
	# 注意 relay.env 是 0600 root:root——systemd 以 root 读 EnvironmentFile，
	# 权限没问题，Caddy 的子进程也能继承到这个变量。
	# 用 drop-in 而不是改 Caddy 官方 unit：apt 升级 Caddy 时官方 unit 会被重写，
	# 改动放 drop-in 才不会丢。
	write_file "$CADDY_DROPIN" 0644 root root <<EOF
# 由 HopDrop deploy/install.sh 生成，请勿手工修改。
# 目的：把 /etc/relay/relay.env 里的 RELAY_DOMAIN 注入 Caddy 进程，
# 供 /etc/caddy/Caddyfile 里的 {\$RELAY_DOMAIN::80} 使用。
[Service]
EnvironmentFile=$ENV_FILE
EOF

	if [ -f "$CADDYFILE" ] && cmp -s "$SRC_DIR/Caddyfile" "$CADDYFILE"; then
		ok "Caddyfile 无变化"
	else
		# 仓库里的 Caddyfile 不含部署专属信息（域名走环境变量），所以可以
		# 安全地整体覆盖。这一点是刻意的：一旦 Caddyfile 里塞进域名，就再也
		# 不能自动更新它了。
		if [ -f "$CADDYFILE" ] && [ "$CHECK" = 0 ]; then
			cp "$CADDYFILE" "$CADDYFILE.hopdrop-prev"
			note "原 Caddyfile 已备份为 $CADDYFILE.hopdrop-prev"
		fi
		copy_in "$SRC_DIR/Caddyfile" "$CADDYFILE" 0644
	fi

	if [ "$CHECK" = 0 ] && [ "$SYS" = 1 ]; then
		sysrun systemctl daemon-reload
		# 用 restart 而不是 reload：drop-in 里新增的 EnvironmentFile 只在
		# 进程重新拉起时才生效，reload 只发信号、不重读 unit 环境。
		# Caddy 的证书缓存在 /var/lib/caddy，重启不会重新申请证书。
		sysrun systemctl enable caddy
		if systemctl is-active --quiet caddy; then
			sysrun systemctl restart caddy
		else
			sysrun systemctl start caddy
		fi
	fi

	# 域名没配时给一条明确提示，别让人对着"证书签不下来"的日志猜。
	local domain=""
	if [ -f "$ENV_FILE" ]; then
		domain="$(awk -F= '/^RELAY_DOMAIN=/ {print $2}' "$ENV_FILE" | tr -d '"' | tail -1)"
	fi
	if [ -z "$domain" ]; then
		warn "RELAY_DOMAIN 未配置：Caddy 会以纯 HTTP（:80）提供服务。"
		warn "此时 HTTPS 不可用，带 Secure 的会话 Cookie 会被浏览器丢弃——"
		warn "也就是能打开页面但配不上对。配好域名解析后填上 RELAY_DOMAIN 再重跑本脚本。"
	fi
}

step_firewall() {
	head_line "防火墙"

	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：ufw"
		return
	fi
	if ! command -v ufw >/dev/null 2>&1; then
		note "未安装 ufw，跳过"
		return
	fi

	# 只放行、不 enable。这条区别很重要：这台机器上跑着共存服务，
	# 如果 ufw 本来是关的，`ufw enable` 会立刻按默认策略（deny incoming）
	# 把别人的端口一起挡掉。开不开防火墙应该由运维决定，不该由部署脚本顺手决定。
	local port
	for port in 80 443; do
		if ufw status 2>/dev/null | grep -qE "^${port}(/tcp)?[[:space:]]+ALLOW"; then
			ok "已放行 $port/tcp"
		else
			sysrun ufw allow "${port}/tcp"
		fi
	done

	if ! ufw status 2>/dev/null | grep -q "Status: active"; then
		warn "ufw 当前未启用，且本脚本**不会**替你启用它。"
		warn "同机有共存服务，贸然 enable 会按默认策略挡掉其他端口。"
		warn "确认过放行清单后自行执行：ufw allow 22/tcp && ufw enable"
	fi
}

step_backup_cron() {
	head_line "备份"

	local src="$SRC_DIR/backup.sh"
	[ -f "$src" ] || {
		warn "找不到 $src，跳过"
		return
	}

	if [ -x "$BACKUP_BIN" ] && cmp -s "$src" "$BACKUP_BIN"; then
		ok "备份脚本无变化"
	else
		copy_in "$src" "$BACKUP_BIN" 0755
	fi

	# 每天 03:00（方案 14.6）。脚本自己判磁盘余量，不足就跳过而不是失败。
	if [ -f "$CRON_FILE" ] && grep -q 'hopdrop-backup' "$CRON_FILE" 2>/dev/null; then
		ok "备份 cron 已存在"
	else
		write_file "$CRON_FILE" 0644 root root <<EOF
# 由 HopDrop deploy/install.sh 生成：每天 03:00 备份数据库（方案 14.6）。
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
0 3 * * * root $BACKUP_BIN
EOF
	fi
}

step_health_timer() {
	head_line "健康检查"

	copy_in "$SRC_DIR/hopdrop-healthcheck.sh" "$HEALTH_BIN" 0755
	copy_in "$SRC_DIR/hopdrop-healthcheck.service" "$HEALTH_UNIT" 0644
	copy_in "$SRC_DIR/hopdrop-healthcheck.timer" "$HEALTH_TIMER" 0644

	if [ "$CHECK" = 0 ] && [ "$SYS" = 1 ]; then
		sysrun systemctl daemon-reload
	fi
}

step_enable() {
	head_line "启用服务"

	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：systemctl enable --now relay"
		return
	fi

	# 代码或 venv 没就位时不要 enable：那只会让 systemd 每 3 秒重启一次，
	# journal 里刷满 ImportError。
	if [ ! -f "$APP_DIR/app/main.py" ] || [ ! -x "$APP_DIR/venv/bin/python" ]; then
		warn "代码或 venv 尚未就位，暂不启动服务。"
		warn "发布代码后重跑本脚本，或直接执行：systemctl enable --now relay hopdrop-healthcheck.timer"
		return
	fi

	sysrun systemctl enable relay
	sysrun systemctl enable --now hopdrop-healthcheck.timer
	if systemctl is-active --quiet relay; then
		sysrun systemctl restart relay
	else
		sysrun systemctl start relay
	fi

	if [ "$CHECK" = 0 ]; then
		sleep 1
		if systemctl is-active --quiet relay; then
			ok "relay 已运行"
		else
			warn "relay 未能启动，查看日志：journalctl -u relay -n 50 --no-pager"
		fi
	fi
}

step_verify() {
	head_line "验收"

	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：健康检查请求"
		return
	fi

	local addr="127.0.0.1:8080"
	if [ -f "$ENV_FILE" ]; then
		local from_env
		from_env="$(awk -F= '/^RELAY_ADDR=/ {print $2}' "$ENV_FILE" | tail -1)"
		[ -n "$from_env" ] && addr="$from_env"
	fi

	if ! command -v curl >/dev/null 2>&1; then
		note "没有 curl，跳过"
		return
	fi

	local body code
	body="$(curl -fsS --max-time 5 "http://$addr/healthz" 2>/dev/null)" && code=0 || code=$?
	if [ "$code" != 0 ]; then
		warn "http://$addr/healthz 请求失败（curl 退出码 $code）。"
		warn "服务可能还在启动，或处于 degraded（degraded 会返回 503，curl -f 视为失败）。"
		say "手工确认：curl -i http://$addr/healthz"
		return
	fi
	ok "健康检查通过：$body"
}

summary() {
	head_line "完成"
	if [ "$CHECK" = 1 ]; then
		say "以上是 --check 的报告，未做任何改动。"
		return
	fi
	cat <<EOF
  服务：systemctl status relay
  日志：journalctl -u relay -f
  健康：curl -i http://127.0.0.1:8080/healthz
  配置：$ENV_FILE
  数据：$STATE_DIR
EOF
}

main() {
	# 部署脚本创建的一切默认私有（0700/0600）。这里显式收紧 umask，
	# 是因为它同时是"目录创建时的默认权限"这一层的兜底：下面每处仍然会
	# 明写权限，两层都做是为了不依赖调用方的 umask 是什么。
	umask 077

	parse_args "$@"
	setup_paths

	printf 'HopDrop 服务器部署%s\n' "$([ "$CHECK" = 1 ] && echo '（--check 模式）' || true)"
	printf '  前缀：%s\n' "$PREFIX"
	if [ "$SYS" = 0 ]; then
		printf '  模式：演练（跳过 apt / ufw / systemd / useradd）\n'
	fi

	step_preflight
	step_packages
	step_timezone_swap
	step_user
	step_dirs
	step_venv
	step_env_file
	step_relay_unit
	step_caddy
	step_firewall
	step_backup_cron
	step_health_timer
	step_enable
	step_verify
	summary
}

main "$@"
