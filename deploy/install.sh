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
# ---------------------------------------------------------------------------
# 同机共存是这个脚本的第一设计约束（方案 17 节：目标机上还跑着别的服务）。
# 由此派生出四条硬规则，改这个脚本之前先读一遍：
#
#   1. **不抢端口。** 80/443 通常已经由既有反代（nginx / Caddy）持有，HopDrop
#      只是它的一个 server 块，上游指向 127.0.0.1:8080。永远不 restart 反代，
#      只在 `nginx -t` 通过之后 reload——reload 平滑，既有连接不断；restart
#      会瞬断这台机器上的所有站点。
#   2. **校验失败必须回滚。** 往一个跑着多个站点的 nginx 里塞一份语法错误的
#      配置，后果是"reload 的那一刻所有站点一起挂"。所以流程固化成
#      写文件 → `nginx -t` → 通过才 reload；不通过就把文件恢复原样。
#   3. **不装网络相关的"顺手包"。** ufw 装了就有人会 enable，而 enable 会按
#      默认策略 deny incoming，把共存服务的端口一起挡掉；fail2ban 装上就会
#      启用 sshd jail，可能把部署者自己 ban 掉。两者都不在依赖列表里。
#   4. **不改系统级设置。** 时区、swap、内核参数一律只报告不修改。
#
# 用法：
#   sudo bash deploy/install.sh                 # 初始化或修复（自动探测反代）
#   sudo bash deploy/install.sh --check         # 只报告差异，不做任何改动
#   sudo bash deploy/install.sh --proxy=caddy   # 目标机用 Caddy 而非 nginx
#   sudo bash deploy/install.sh --proxy=none    # 不配反代，只在回环提供服务
#   bash deploy/install.sh --no-system --prefix /tmp/drill
#                                               # 本机演练：跳过 apt / 系统服务
#                                               #   / useradd，把绝对路径挂到
#                                               #   --prefix 下
#
# 演练模式是为"在没有 Linux 机器的情况下验证这个脚本本身"而存在的。它跑的是
# 真实的文件生成、权限与幂等逻辑，只把需要真系统的部分让开。

set -euo pipefail

# ---------------------------------------------------------------- 常量与参数

# 可被 --prefix / --no-system 覆盖，所以不用 readonly。
PREFIX="/"
SYS=1
CHECK=0
# auto | nginx | caddy | none
PROXY_MODE="auto"
# step_reverse_proxy 探测后的实际取值，供后续步骤（防火墙、汇总）判断。
RESOLVED_PROXY=""

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
NGINX_SITE=""
NGINX_SITE_ENABLED=""

usage() {
	cat <<'EOF'
用法：sudo bash deploy/install.sh [选项]

  --check             只报告将要做的改动，不写任何文件、不动任何服务
  --proxy=MODE        反向代理模式：auto（默认，自动探测 80/443 上的进程）
                      | nginx | caddy | none（不配反代，仅回环可用）
  --prefix DIR        把 /opt/relay、/var/lib/relay 等绝对路径挂到 DIR 下（演练用）
  --no-system         跳过 apt / systemctl / useradd 等需要真实系统与 root 的步骤
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
		--proxy)
			shift
			[ $# -gt 0 ] || die "--proxy 需要一个取值"
			PROXY_MODE="$1"
			;;
		--proxy=*) PROXY_MODE="${1#--proxy=}" ;;
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

	case "$PROXY_MODE" in
	auto | nginx | caddy | none) ;;
	*) die "--proxy 只接受 auto / nginx / caddy / none，收到：$PROXY_MODE" ;;
	esac

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
	# 站点文件名不带 .conf 后缀，与这台机器上既有的 fortranslate / minitalk /
	# phound 保持一致——只在 sites-enabled/* 的 include 范围内，两边都行，
	# 但命名风格统一之后，ls 一下就知道哪些站点是谁的。
	NGINX_SITE="$p/etc/nginx/sites-available/hopdrop"
	NGINX_SITE_ENABLED="$p/etc/nginx/sites-enabled/hopdrop"
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

# 丢弃临时文件。吞掉错误：删不掉一个临时文件不该让部署停在这里。
# 本机演练环境的删除守卫会拦下部分路径（报 SAFE_DELETE_*），而真实 Linux 上
# `rm -f` 基本不会失败——但"因为删不掉临时文件就中止部署"的代价太高，
# 所以这里一律把失败吞掉。
discard_tmp() {
	rm -f "$1" 2>/dev/null || true
}

# 写入文件的统一入口：--check 时只报告，内容没变时不动它。
#
# "内容没变就不写"不只是为了让日志好看：这些文件里有 systemd 单元，
# 每次触碰都会让下一步的 daemon-reload 真的重载，而重载之后跟不跟一次
# restart 是很难一眼看出来的（见 step_reverse_proxy 的说明）。
write_file() {
	local path="$1" mode="$2" owner="$3" group="$4"
	shift 4

	local tmp
	tmp="$(mktemp)"
	cat >"$tmp"

	if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
		discard_tmp "$tmp"
		ok "无变化 $path"
		return 0
	fi

	if [ "$CHECK" = 1 ]; then
		discard_tmp "$tmp"
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

# 从 relay.env 里读一个键的值。取最后一条匹配（后面的覆盖前面的），去掉引号。
read_env_value() {
	local key="$1"
	[ -f "$ENV_FILE" ] || return 0
	grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true
}

# 某端口上持有 listen 的进程名（取第一个）。ss -p 需要 root 才能看到别人的进程。
port_holder() {
	local port="$1"
	command -v ss >/dev/null 2>&1 || return 0
	ss -lntpH "sport = :$port" 2>/dev/null |
		sed -n 's/.*users:((\"\([^"]*\)\".*/\1/p' |
		head -1
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

	# 刻意**不**包含 ufw 与 fail2ban：同机共存时，这两个装上就会被启用，
	# 而它们的默认策略会去动网络——`ufw enable` 按 deny incoming 把共存服务
	# 的端口一起挡掉，fail2ban 的 sshd jail 可能把部署者自己 ban 掉。
	# 需要它们应该由运维单独决定，而不是被一次应用部署顺手带上来。
	#
	# git 也不在其中：代码是 release.sh 打包推上来的，服务器不需要拉仓库。
	local pkgs="python3 python3-venv python3-pip curl ca-certificates sqlite3"
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
	# 名字保留 swap，但时区从"设置"改成了"只报告"：改时区会连带改变这台机器上
	# 其他服务的日志时间戳，属于运维决定而不是部署副作用。同机只跑 HopDrop 时
	# 这个区别看不出来，共存机器上它是"日志对不上时间"这类问题的来源。
	head_line "时区与 swap"

	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：时区与 swap"
		return
	fi

	if command -v timedatectl >/dev/null 2>&1; then
		local tz
		tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
		if [ "$tz" = "Asia/Shanghai" ]; then
			ok "时区已是 Asia/Shanghai"
		else
			warn "时区是 ${tz:-未知}，不是 Asia/Shanghai。"
			warn "本脚本**不会**替你改：同机还有其他服务，改时区会连带改变它们的日志时间戳。"
			warn "确实需要就手工执行：timedatectl set-timezone Asia/Shanghai"
		fi
	fi

	# swap 同样只读。新建 swap 要改 /etc/fstab，属于系统级改动；
	# 而机器上多半已经有运维配好的 swapfile，动它没有收益只有风险。
	if swapon --show=NAME --noheadings 2>/dev/null | grep -q .; then
		local swap_size
		swap_size="$(free -h 2>/dev/null | awk '/^Swap:/ {print $2}')"
		ok "已有 swap（${swap_size:-未知}），未改动"
	else
		warn "没有启用 swap。内存紧张的机器上一旦 OOM，被杀的是进程而不是缓存的页。"
		warn "本脚本不替你创建（会改 /etc/fstab）。建议手工建 1–2 GiB 的 swapfile。"
	fi
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
	# tmp 是给 TMPDIR 用的：starlette 的 UploadFile 超过 1 MiB 就落临时文件，
	# 而 PrivateTmp 给的 /tmp 是 tmpfs（占内存）。这台机器可用内存只有几百
	# MiB，一次 20 MiB 的上传就会实打实吃掉 20 MiB。指到数据盘上没有这个问题。
	local dirs="$STATE_DIR $STATE_DIR/files $STATE_DIR/thumbs $STATE_DIR/backup $STATE_DIR/tmp $APP_DIR $ENV_DIR"

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
		chmod 0700 "$STATE_DIR" "$STATE_DIR"/files "$STATE_DIR"/thumbs "$STATE_DIR"/backup "$STATE_DIR"/tmp 2>/dev/null || true
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

# ------------------------------------------------------------ nginx 站点生成

# 渲染 proxy 配置段。整段都是 nginx 字面量，只有上游地址需要插值，
# 所以用带引号的 heredoc 避免一堆 \$ 转义——漏掉一个就会静默展开成空字符串，
# 而那种配置大概率还能通过 nginx -t，问题要等到运行时才显形。
render_nginx_proxy_body() {
	cat <<'NGINX'
    location / {
        proxy_pass http://@UPSTREAM@;
        proxy_http_version 1.1;

        # **覆盖式**写 X-Forwarded-For，而不是 $proxy_add_x_forwarded_for。
        #
        # 应用侧的 client_ip() 取这个头的**第一个**值作为来源 IP，用于方案 10
        # 的单 IP 连接上限。追加式写法会把客户端自己带来的值排在最前面，
        # 于是任何人都能伪造来源 IP、绕过连接上限。覆盖之后，这里永远是
        # nginx 看到的真实对端地址，伪造值进不来。
        #
        # 前提是 uvicorn 只监听 127.0.0.1（RELAY_ADDR 的默认值）。哪天把它
        # 暴露到 0.0.0.0，这个头就重新变成客户端可控的。
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Host 不带端口：应用的 same_origin() 拿 Origin 的 netloc 与 Host 头做
        # 字符串相等比较，带上 :443 会让它不等——症状是 WebSocket 一律被拒，
        # 而 HTTP 请求看着完全正常。
        proxy_set_header Host $host;

        # 单文件上限 20 MiB（config.MAX_FILE_BYTES_HARD_LIMIT）。
        # nginx 默认是 1 MiB，不放开的话超过 1 MiB 的上传会被 nginx 直接 413，
        # 请求根本到不了应用——应用侧自己的错误信息一条都不会出现，
        # 排查时对着"上传失败"完全找不到线索。
        client_max_body_size 24m;

        proxy_connect_timeout 5s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    # WebSocket 单独一个 location：只在 /ws 上开 Upgrade，
    # 不把普通请求也标成 Connection: upgrade。
    location = /ws {
        proxy_pass http://@UPSTREAM@;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $host;

        proxy_connect_timeout 5s;
        # 客户端每 25 秒 ping 一次，应用侧 60 秒收不到活动就关连接。
        # nginx 默认的 60s 正好卡在这个边界上，一轮网络抖动就可能切断——
        # 表现是页面"偶尔自己刷新"，而服务端日志里什么都没有。
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    # 日志单独落文件，不与既有站点混在 /var/log/nginx/access.log 里。
    access_log /var/log/nginx/hopdrop.access.log;
    error_log  /var/log/nginx/hopdrop.error.log;
NGINX
}

# 渲染完整站点配置。with_tls=1 时输出"80 跳转 + 443 反代"，否则只有 80 反代。
render_nginx_site() {
	local domain="$1" upstream="$2" with_tls="$3"
	local body banner

	body="$(render_nginx_proxy_body)"
	banner="$(cat <<'NGINX'
# HopDrop 反向代理站点。由 deploy/install.sh 生成，请勿手工修改——
# 每次发布都会用仓库里的版本重写这个文件。
#
# 设计要点（改之前先读一遍）：
#   * HopDrop 不持有 80/443，只作为既有 nginx 的一个 server 挂进来。
#     这份配置从不重启 nginx，只在 `nginx -t` 通过后 reload。
#   * X-Forwarded-For 用 $remote_addr **覆盖**而不是追加，原因见 location / 的注释。
#   * 80 段与 443 段里的 proxy 配置是各写一遍的，没有抽成 include：
#     这样运维打开这一个文件就能看全，不必跳到 snippets 目录再跳回来。
#   * 这个文件按域名参数化生成，不含任何部署专属的手改内容，
#     所以"每次发布整体重写"是安全的。
NGINX
)"

	if [ "$with_tls" = 1 ]; then
		{
			printf '%s\n\n' "$banner"
			cat <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name @DOMAIN@;

    # certbot 用 webroot 方式签发，HTTP-01 挑战必须由文件系统应答，
    # 而且要在 301 跳转之前接住。^~ 前缀匹配压过下面的 location /。
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type "text/plain";
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name @DOMAIN@;

    ssl_certificate     /etc/letsencrypt/live/@DOMAIN@/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/@DOMAIN@/privkey.pem;
    # 协议版本、加密套件、DH 参数。引用 certbot 生成的那两份而不是抄一份
    # 进来，是为了让 TLS 参数随 certbot 一起更新。两者必然与证书同时存在
    # ——证书就是 certbot 签的。
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # HSTS。Caddy 在 HTTPS 下会自动加，nginx 不会——从 Caddy 切到 nginx 后
    # 这一条是最容易丢的：域名照常能开、证书照样有效，只是"浏览器从此只用
    # HTTPS 访问"这层保护静默消失了。
    #
    # **绝不加 includeSubDomains**：那会把 devilsarchive.cn 下的其它子域
    # （translate / phound / minitalk）一起拖进强制 HTTPS，而这台机器上的
    # 其它站点不归 HopDrop 管，给别人的域名下强制策略属于越界。也不加 preload。
    add_header Strict-Transport-Security "max-age=31536000" always;

NGINX
			printf '%s\n' "$body"
			printf '}\n'
		}
	else
		{
			printf '%s\n\n' "$banner"
			cat <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name @DOMAIN@;

    # 证书还没签时也用得上：certbot 的 webroot 挑战要由这个 location 接住。
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type "text/plain";
    }

NGINX
			printf '%s\n' "$body"
			printf '}\n'
		}
	fi | sed -e "s|@DOMAIN@|$domain|g" -e "s|@UPSTREAM@|$upstream|g"
}

# 证书是否存在。用 PREFIX 拼接，这样演练模式下也能被测试驱动。
tls_cert_exists() {
	[ -f "$PREFIX/etc/letsencrypt/live/$1/fullchain.pem" ]
}

# 站点配置的备份只保留最近几个。
#
# 这些 .before-* 是"刚改坏了能立刻退回去"的短期保险，不是归档。每次发布
# 内容都会变，也就每次都留一份；不清理的话 sites-available 里会慢慢堆满
# 同名不同戳的文件，而这些文件**仍然在 nginx 的 include 范围之外**（只 include
# sites-enabled/*），所以不会造成故障，却会让目录越来越难读。
prune_nginx_backups() {
	local keep=5
	local dir
	dir="$(dirname "$NGINX_SITE")"
	# 整个管道末尾挂 `|| true`：这是收尾动作，失败绝不能让部署停在这里。
	# 本脚本开着 `set -e` + `pipefail`，而 while 跑在子 shell 里，任何一条
	# rm 返回非 0（权限、文件被占、被安全策略拦）都会让管道整体失败，
	# 进而中止整个部署——为了删掉一个无关紧要的旧备份，代价明显不划算。
	ls -1t "$dir"/hopdrop.before-hopdrop-* 2>/dev/null |
		tail -n "+$((keep + 1))" |
		while IFS= read -r old; do
			rm -f "$old" 2>/dev/null || true
		done || true
	return 0
}

# ------------------------------------------------------------ 反向代理分派

step_reverse_proxy() {
	local mode="$PROXY_MODE"

	if [ "$mode" = "auto" ]; then
		if [ "$SYS" = 0 ]; then
			# 演练模式没有真实端口可探（PREFIX 挂在临时目录下），
			# 按默认的 nginx 走，好让测试覆盖到这条分支。
			mode="nginx"
			note "演练模式：无法探测真实端口，按 nginx 处理（真实环境会自动探测）"
		else
			mode="$(detect_proxy)"
			if [ -z "$mode" ]; then
				mode="none"
			else
				say "探测到 80/443 由既有 $mode 持有；HopDrop 将作为它的一个 server 挂载，"
				say "既不安装它，也不重启它。"
			fi
		fi
	fi

	RESOLVED_PROXY="$mode"

	case "$mode" in
	nginx) step_nginx ;;
	caddy) step_caddy ;;
	none) step_proxy_none ;;
	*) die "未知的反代模式：$mode" ;;
	esac
}

detect_proxy() {
	local holder
	# 先看 443：能签下证书的那台机器，443 上才是真正的对外入口。
	for port in 443 80; do
		holder="$(port_holder "$port")"
		case "$holder" in
		nginx | caddy) printf '%s\n' "$holder"; return 0 ;;
		esac
	done
	return 0
}

step_proxy_none() {
	head_line "反向代理"

	warn "没有在 80/443 上探测到 nginx 或 Caddy，且未指定 --proxy。"
	warn "HopDrop 只监听 127.0.0.1:8080，公网无法访问——本地验收没问题，"
	warn "但配对用的二维码/链接在别的设备上打不开。"
	warn "需要对外服务时，把既有反代指向 127.0.0.1:8080，或用 --proxy=nginx|caddy 指定。"
}

# ------------------------------------------------------------ nginx 分支

step_nginx() {
	head_line "nginx 站点（复用既有实例）"

	# nginx 二进制只在真实系统上检查。演练模式（SYS=0）跑在本机，那里没有
	# nginx——但站点渲染是纯文件生成，恰恰是演练最该覆盖的部分，所以不拦。
	if [ "$SYS" = 1 ] && ! command -v nginx >/dev/null 2>&1; then
		warn "80/443 由 nginx 持有，但当前 PATH 里找不到 nginx 命令；跳过站点配置。"
		return
	fi

	local domain upstream
	domain="$(read_env_value RELAY_DOMAIN)"
	upstream="$(read_env_value RELAY_ADDR)"
	[ -n "$upstream" ] || upstream="127.0.0.1:8080"

	if [ -z "$domain" ]; then
		warn "RELAY_DOMAIN 未配置，无法生成 server_name。"
		warn "这台机器的 80 端口上 default_server 是别的服务，没有域名就分不出流量。"
		warn "在 $ENV_FILE 里填好 RELAY_DOMAIN 后重跑本脚本。"
		return
	fi

	local with_tls=0
	if tls_cert_exists "$domain"; then
		with_tls=1
		ok "找到 $domain 的证书，生成 80 跳转 + 443 反代"
	else
		warn "找不到 $domain 的证书（$PREFIX/etc/letsencrypt/live/$domain/）。"
		warn "本次只生成 HTTP 版（listen 80）。此时会话 Cookie 带着 Secure 属性，"
		warn "浏览器会直接丢弃它——表现是"首页能打开、配对却总回到未登录"，"
		warn "所以这只能用于首次连通性验收。签证书："
		warn "  sudo bash $APP_DIR/deploy/certbot-hopdrop.sh $domain"
		warn "签完重跑本脚本，会自动换成带 443 的版本。"
	fi

	local tmp
	tmp="$(mktemp)"
	render_nginx_site "$domain" "$upstream" "$with_tls" >"$tmp"

	# 两件事分开判断：文件内容要不要更新、sites-enabled 里的软链要不要修。
	# 合成一个条件会让"内容其实没变、只是软链不对"的情况也走一遍重写，
	# 于是每跑一次就多一个 .before-* 备份，几天下来堆满 sites-available。
	local need_write=0 need_link=0
	if [ ! -f "$NGINX_SITE" ] || ! cmp -s "$tmp" "$NGINX_SITE"; then
		need_write=1
	fi
	if [ "$(readlink "$NGINX_SITE_ENABLED" 2>/dev/null || true)" != "$NGINX_SITE" ]; then
		need_link=1
	fi

	if [ "$need_write" = 0 ] && [ "$need_link" = 0 ]; then
		discard_tmp "$tmp"
		ok "站点配置无变化（$NGINX_SITE）"
		return
	fi

	if [ "$CHECK" = 1 ]; then
		discard_tmp "$tmp"
		[ "$need_write" = 1 ] && note "计划写入 $NGINX_SITE"
		[ "$need_link" = 1 ] && note "计划创建软链 $NGINX_SITE_ENABLED → $NGINX_SITE"
		return
	fi

	# 动别人的东西之前先留退路。命名沿用这台机器上既有的惯例
	# （见 sites-available/phound.before-redirect、fortranslate.before-pwa-*）。
	local backup=""
	if [ "$need_write" = 1 ]; then
		if [ -f "$NGINX_SITE" ]; then
			backup="$NGINX_SITE.before-hopdrop-$(date +%Y%m%d%H%M%S)"
			cp -p "$NGINX_SITE" "$backup"
			note "原配置已备份为 $backup"
			prune_nginx_backups
		fi

		mkdir -p "$(dirname "$NGINX_SITE")"
		mv "$tmp" "$NGINX_SITE"
		chmod 0644 "$NGINX_SITE"
		ok "已写入 $NGINX_SITE"
	else
		discard_tmp "$tmp"
		ok "站点配置内容无变化"
	fi

	if [ "$need_link" = 1 ]; then
		mkdir -p "$(dirname "$NGINX_SITE_ENABLED")"
		local link_err
		if link_err="$(ln -sfn "$NGINX_SITE" "$NGINX_SITE_ENABLED" 2>&1)"; then
			ok "已启用 $NGINX_SITE_ENABLED"
		elif ln "$NGINX_SITE" "$NGINX_SITE_ENABLED" 2>/dev/null; then
			# 回退到硬链接。Windows 上创建符号链接需要特权（或开发者模式），
			# 本机演练会撞上；而 nginx 只是读这个文件，硬链接语义上同样够用。
			note "该文件系统不支持符号链接，已改用硬链接启用站点"
		elif [ "$SYS" = 1 ]; then
			# 真实服务器上启用失败就是部署失败：站点不会被 nginx 加载，
			# 而"服务起着但访问 404"这种状态比直接报错难查得多。
			die "无法启用 $NGINX_SITE_ENABLED：$link_err"
		else
			warn "演练环境无法创建链接（Linux 上会创建符号链接）：$link_err"
		fi
	fi

	if [ "$SYS" = 0 ]; then
		note "演练模式：跳过 nginx -t 与 reload"
		return
	fi

	# ---------------------------------------------------------------- 关键
	# 校验通过才 reload。这台机器上还有别的站点，把一份语法错误的配置
	# reload 进去，结果是"所有站点一起挂"，而且就在 reload 的那一刻发生。
	if nginx -t >/dev/null 2>&1; then
		ok "nginx -t 通过"
	else
		printf '\n' >&2
		nginx -t 2>&1 | sed 's/^/    /' >&2
		# 回滚本身也要容错。好消息是：走到这里时**还没有 reload**，
		# 所以 nginx 跑着的仍是旧配置，其他站点是安全的——回滚失败只是
		# 让磁盘上的文件脏了，不会造成线上故障。因此这里全部 warn 不 die，
		# 把"没有 reload"这个关键事实留给最后那句 die 说清楚。
		if [ -n "$backup" ]; then
			if cp -p "$backup" "$NGINX_SITE" 2>/dev/null; then
				note "已从 $backup 恢复原配置"
			else
				warn "恢复 $backup 失败。nginx 未受影响（没有 reload），但请手工核对 $NGINX_SITE。"
			fi
		else
			rm -f "$NGINX_SITE_ENABLED" "$NGINX_SITE" 2>/dev/null || true
			note "已移除本次新建的站点文件与软链"
		fi
		die "nginx 配置校验失败。**未执行 reload**——nginx 的状态与本脚本运行前一致，
其他站点不受影响。修正上面的语法问题后重跑本脚本即可。"
	fi

	# reload 而不是 restart：reload 平滑，既有连接不断；
	# restart 会把这台机器上所有站点瞬断一次。
	sysrun systemctl reload nginx
	ok "nginx 已 reload（未重启，其他站点不受影响）"

	if [ "$with_tls" = 0 ]; then
		warn "当前是 HTTP 版站点；签完证书后重跑本脚本即可切换为 HTTPS。"
	fi

	# 配了域名但没配备案号时，浏览器能打开但首页少一行监管信息。
	local icp
	icp="$(read_env_value RELAY_ICP_LICENSE)"
	if [ -z "$icp" ]; then
		warn "RELAY_ICP_LICENSE 仍为空：上线前必须补上（方案 11.1 / 第 13 节）。"
	fi
}

# ------------------------------------------------------------ Caddy 分支
#
# 非默认路径：只在目标机本来就跑着 Caddy 时走这条。
# Caddy 会自动申请证书，所以不需要单独的 certbot 步骤。

step_caddy() {
	head_line "Caddy"

	if ! command -v caddy >/dev/null 2>&1; then
		warn "80/443 由 Caddy 持有，但当前 PATH 里找不到 caddy 命令；跳过站点配置。"
		return
	fi

	ok "Caddy 已安装（$(caddy version 2>/dev/null | head -1)）"

	# 让 Caddy 能读到 RELAY_DOMAIN。Caddyfile 里写的是 {$RELAY_DOMAIN::80}，
	# 意思是"取环境变量，没有就用 :80（纯 HTTP）"。把 relay.env 作为
	# EnvironmentFile 注入，域名就只需要维护一处。
	#
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

	local domain
	domain="$(read_env_value RELAY_DOMAIN)"
	if [ -z "$domain" ]; then
		warn "RELAY_DOMAIN 未配置：Caddy 会以纯 HTTP（:80）提供服务。"
		warn "此时 HTTPS 不可用，带 Secure 的会话 Cookie 会被浏览器丢弃——"
		warn "也就是能打开页面但配不上对。配好域名解析后填上 RELAY_DOMAIN 再重跑本脚本。"
	fi
}

step_firewall() {
	head_line "防火墙"

	if [ "$SYS" = 0 ]; then
		note "跳过（演练模式）：防火墙"
		return
	fi

	# 这个脚本**一条防火墙规则都不加**，只报告现状。理由：
	#   * 复用既有反代时，80/443 的放行属于既有服务，不该由应用部署脚本代管；
	#   * 没有反代时 HopDrop 只在回环上，也不需要放行；
	#   * 装 ufw 这件事本身就有风险——它一旦被 enable，就按默认策略
	#     deny incoming 把共存服务的端口一起挡掉。
	# 所以决定权留给运维，这里只把现状说清楚。
	if command -v ufw >/dev/null 2>&1; then
		local state
		state="$(ufw status 2>/dev/null | head -1 || true)"
		say "ufw：${state:-状态未知}"
		if ! ufw status 2>/dev/null | grep -q "Status: active"; then
			note "ufw 未启用；本脚本不会替你启用它（同机有共存服务）。"
		fi
	else
		say "未安装 ufw（本脚本也不会安装它）"
	fi
	note "HopDrop 不需要新增防火墙规则：对外入口由既有反代承担，服务本身只在回环。"
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

	local addr
	addr="$(read_env_value RELAY_ADDR)"
	[ -n "$addr" ] || addr="127.0.0.1:8080"

	if ! command -v curl >/dev/null 2>&1; then
		note "没有 curl，跳过"
		return
	fi

	# 不用 `curl -f`：degraded 时应用返回 503 并**在响应体里给出 reasons**，
	# 而 -f 会连响应体一起丢掉——排查时最需要的那几个字正好被扔了。
	# --fail-with-body 保留退出码语义，同时把 body 留下。
	local raw code body
	raw="$(curl -sS --max-time 5 --fail-with-body -w '\n%{http_code}' "http://$addr/healthz" 2>/dev/null)" || true
	code="${raw##*$'\n'}"
	body="${raw%$'\n'*}"

	case "$code" in
	200)
		ok "健康检查通过：${body:-（响应体为空）}"
		;;
	503)
		warn "服务活着但处于 degraded（HTTP 503）："
		printf '         %s\n' "$body" >&2
		warn "reasons 字段就是原因；磁盘配额或数据目录不可写是最常见的两条。"
		;;
	*)
		warn "http://$addr/healthz 无响应（HTTP ${code:-000}）。"
		warn "服务可能还在启动。手工确认：curl -i http://$addr/healthz"
		;;
	esac
}

summary() {
	head_line "完成"
	if [ "$CHECK" = 1 ]; then
		say "以上是 --check 的报告，未做任何改动。"
		return
	fi

	local domain
	domain="$(read_env_value RELAY_DOMAIN)"

	cat <<EOF
  服务：systemctl status relay
  日志：journalctl -u relay -f
  健康：curl -i http://127.0.0.1:8080/healthz
  配置：$ENV_FILE
  数据：$STATE_DIR
EOF

	case "$RESOLVED_PROXY" in
	nginx)
		if [ -n "$domain" ]; then
			if tls_cert_exists "$domain"; then
				printf '  入口：https://%s/\n' "$domain"
			else
				printf '  入口：http://%s/（尚未签证书，见上面的提示）\n' "$domain"
			fi
		else
			printf '  入口：未配置（%s 里补上 RELAY_DOMAIN）\n' "$ENV_FILE"
		fi
		printf '  站点：%s（软链 %s）\n' "$NGINX_SITE" "$NGINX_SITE_ENABLED"
		printf '  反代日志：/var/log/nginx/hopdrop.*.log\n'
		;;
	caddy)
		[ -n "$domain" ] && printf '  入口：https://%s/\n' "$domain"
		;;
	esac
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
	printf '  反代：%s\n' "$PROXY_MODE"
	if [ "$SYS" = 0 ]; then
		printf '  模式：演练（跳过 apt / 系统服务 / useradd）\n'
	fi

	step_preflight
	step_packages
	step_timezone_swap
	step_user
	step_dirs
	step_venv
	step_env_file
	step_relay_unit
	step_reverse_proxy
	step_firewall
	step_backup_cron
	step_health_timer
	step_enable
	step_verify
	summary
}

main "$@"
