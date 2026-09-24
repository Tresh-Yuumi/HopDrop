#!/usr/bin/env bash
#
# HopDrop 回滚（方案 18 第 10 条要求"回滚方法"）。
#
# **在服务器上跑**。代码回滚是默认动作，数据库回滚要显式指定备份文件。
#
#   bash deploy/rollback.sh                 # 代码回滚到上一次发布，并重启
#   bash deploy/rollback.sh --list          # 列出可用的代码备份与数据库备份
#   bash deploy/rollback.sh --dry-run       # 只看会做什么
#   bash deploy/rollback.sh --db /var/lib/relay/backup/relay-20260925-030000.db --yes
#
# 为什么必须删 -wal / -shm：SQLite 的 WAL 文件里可能存着还没合并进主库的
# 已提交事务。把主库换成旧备份却把新 WAL 留在原地，下次启动时 SQLite 会把
# 新的事务重放到旧库上——得到一个两边都不像的数据库。删掉 WAL 等于明确
# 放弃那部分未合并事务，这正是"回滚到某个时间点"应有的语义。

set -euo pipefail

APP_DIR="${RELAY_APP_DIR:-/opt/relay}"
PREV_DIR="${RELAY_PREV_DIR:-/opt/relay.prev}"
DATA_DIR="${RELAY_DATA_DIR:-/var/lib/relay}"
BACKUP_DIR="$DATA_DIR/backup"
SERVICE="${RELAY_SERVICE:-relay}"
ENV_FILE="${RELAY_ENV_FILE:-/etc/relay/relay.env}"

MODE="code"
DB_FILE=""
ASSUME_YES=0
DRY_RUN=0

usage() {
	cat <<'EOF'
用法：bash deploy/rollback.sh [选项]

  --list              列出可用的代码备份与数据库备份
  --db FILE           用指定备份恢复数据库（与代码回滚一起做）
  --code-only         只回滚代码（默认行为，显式写出来更清楚）
  --yes               跳过数据库恢复的确认提示（脚本化时用）
  --dry-run           只打印将要执行的操作
  -h, --help          显示本帮助
EOF
}

die() {
	printf '错误：%s\n' "$*" >&2
	exit 1
}
step() { printf '\n== %s\n' "$*"; }
say() { printf '  %s\n' "$*"; }

while [ $# -gt 0 ]; do
	case "$1" in
	--list) MODE="list" ;;
	--db)
		shift
		DB_FILE="${1:-}"
		MODE="full"
		;;
	--db=*)
		DB_FILE="${1#--db=}"
		MODE="full"
		;;
	--code-only) MODE="code" ;;
	--yes) ASSUME_YES=1 ;;
	--dry-run) DRY_RUN=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*) die "未知参数：$1（--help 看用法）" ;;
	esac
	shift
done

read_manifest_version() {
	local dir="$1"
	if [ -f "$dir/MANIFEST.txt" ]; then
		awk -F= '/^version=/ {print $2}' "$dir/MANIFEST.txt" | tail -1
	else
		printf '未知'
	fi
}

list_backups() {
	step "代码备份"
	if [ -d "$PREV_DIR/app" ]; then
		say "$PREV_DIR（版本 $(read_manifest_version "$PREV_DIR")）"
	else
		say "无（$PREV_DIR 不存在，说明还没有被任何一次发布覆盖过）"
	fi

	step "数据库备份"
	if [ -d "$BACKUP_DIR" ] && ls -1 "$BACKUP_DIR"/relay-*.db >/dev/null 2>&1; then
		local f size
		for f in $(ls -1t "$BACKUP_DIR"/relay-*.db); do
			size="$(du -h "$f" 2>/dev/null | awk '{print $1}')"
			say "$f  ($size)"
		done
	else
		say "无（$BACKUP_DIR 下没有 relay-*.db）"
	fi

	step "当前版本"
	if [ -f "$APP_DIR/MANIFEST.txt" ]; then
		say "运行中：$(read_manifest_version "$APP_DIR")"
		say "MANIFEST：$(tr '\n' ' ' <"$APP_DIR/MANIFEST.txt")"
	else
		say "找不到 $APP_DIR/MANIFEST.txt"
	fi
}

if [ "$MODE" = "list" ]; then
	list_backups
	exit 0
fi

# ---------------------------------------------------------------- 代码回滚

step "代码回滚"

if [ ! -d "$PREV_DIR/app" ]; then
	die "$PREV_DIR 下没有 app/，没有可回滚的代码。
这可能是首次部署，或者上一次 rollout 没走 deploy/release.sh。"
fi

say "从 $(read_manifest_version "$PREV_DIR") 回滚到 ${APP_DIR}（当前 $(read_manifest_version "$APP_DIR")）"

if [ "$DRY_RUN" = 1 ]; then
	say "（dry-run）将把 $PREV_DIR/{app,web,migrations,deploy} 复制回 $APP_DIR 并重启 $SERVICE"
else
	for item in app web migrations deploy; do
		if [ ! -d "$PREV_DIR/$item" ]; then
			say "跳过 $item（备份里没有）"
			continue
		fi
		rm -rf "$APP_DIR/$item"
		cp -a "$PREV_DIR/$item" "$APP_DIR/$item"
		say "已恢复 $item/"
	done

	for item in requirements.txt relay.env.example MANIFEST.txt; do
		if [ -f "$PREV_DIR/$item" ]; then
			cp -a "$PREV_DIR/$item" "$APP_DIR/$item"
			say "已恢复 $item"
		fi
	done

	if id -u relay >/dev/null 2>&1; then
		chown -R relay:relay "$APP_DIR/app" "$APP_DIR/web" "$APP_DIR/migrations" 2>/dev/null || true
	fi
	chmod 755 "$APP_DIR/deploy"/*.sh 2>/dev/null || true
	say "代码已回滚"
fi

# ---------------------------------------------------------------- 数据库回滚

if [ "$MODE" = "full" ]; then
	step "数据库回滚"

	[ -n "$DB_FILE" ] || die "--db 需要一个备份文件路径"
	if [ ! -f "$DB_FILE" ]; then
		# 允许只给文件名，自动到备份目录里找。
		if [ -f "$BACKUP_DIR/$DB_FILE" ]; then
			DB_FILE="$BACKUP_DIR/$DB_FILE"
		else
			die "找不到备份文件：$DB_FILE"
		fi
	fi

	say "将用 $DB_FILE 覆盖 $DATA_DIR/relay.db"
	say "当前数据库会先另存为 relay.db.before-rollback-<时间戳>"

	if [ "$ASSUME_YES" = 0 ] && [ "$DRY_RUN" = 0 ]; then
		printf '\n这是破坏性操作：回滚后**备份之后**的所有消息与文件记录都会消失。\n'
		printf '确认请输入 yes：'
		read -r answer
		[ "$answer" = "yes" ] || die "已取消"
	fi

	if [ "$DRY_RUN" = 1 ]; then
		say "（dry-run）将停服务、换库、清 WAL、重启、做完整性检查"
	else
		if command -v systemctl >/dev/null 2>&1; then
			systemctl stop "$SERVICE" || true
			say "已停止 $SERVICE"
		fi

		stamp="$(date +%Y%m%d-%H%M%S)"
		# 先备份当前库，让这次回滚本身也是可逆的。
		if [ -f "$DATA_DIR/relay.db" ]; then
			cp -a "$DATA_DIR/relay.db" "$DATA_DIR/relay.db.before-rollback-$stamp"
			say "当前库已另存为 relay.db.before-rollback-$stamp"
		fi

		# 见文件顶部：不删 WAL 会把新事务重放到旧库上。
		rm -f "$DATA_DIR/relay.db-wal" "$DATA_DIR/relay.db-shm"
		cp -a "$DB_FILE" "$DATA_DIR/relay.db"
		chmod 600 "$DATA_DIR/relay.db"
		if id -u relay >/dev/null 2>&1; then
			chown relay:relay "$DATA_DIR/relay.db"
		fi
		say "已替换 relay.db"

		if command -v sqlite3 >/dev/null 2>&1; then
			if sqlite3 "$DATA_DIR/relay.db" "PRAGMA integrity_check;" | grep -qi '^ok$'; then
				say "integrity_check 通过"
			else
				printf '警告：integrity_check 未通过，库可能已损坏\n' >&2
			fi
		fi
	fi
fi

# ---------------------------------------------------------------- 重启

step "重启服务"

if [ "$DRY_RUN" = 1 ]; then
	say "（dry-run）将执行 systemctl restart $SERVICE"
	exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
	systemctl restart "$SERVICE"
	sleep 2
	if systemctl is-active --quiet "$SERVICE"; then
		say "$SERVICE 已运行"
	else
		printf '警告：%s 未能启动，查看：journalctl -u %s -n 50 --no-pager\n' "$SERVICE" "$SERVICE" >&2
	fi
fi

# 回滚后 /opt/relay.prev 仍是回滚前的版本，留着可以再滚回去。
printf '\n回滚完成。\n'
printf '  当前版本：%s\n' "$(read_manifest_version "$APP_DIR")"
printf '  反向操作：再执行一次本脚本即可回到回滚前的版本\n'
printf '  健康检查：curl -sS -i http://127.0.0.1:8080/healthz\n'

if [ -f "$ENV_FILE" ]; then
	# 新版本可能引入了老代码不认识的配置项，老代码会直接忽略——这本身没问题，
	# 但如果老代码依赖某个新版本才有的表或列，那必须一起回滚数据库。
	say "提示：relay.env 未参与回滚（里面是部署专属信息）。"
fi
