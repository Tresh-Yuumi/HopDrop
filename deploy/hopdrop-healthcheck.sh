#!/usr/bin/env bash
#
# HopDrop 健康检查（方案 14.5）。由 hopdrop-healthcheck.timer 每分钟调用。
#
# 它做的判断很简单：请求 /healthz，把状态与原因记进 journal 和状态文件。
# 复杂的地方在于**默认不做自动重启**，这是刻意的：
#
#   /healthz 返回 degraded 的原因里，磁盘余量不足、数据目录超配额、清理任务
#   长时间失败这三类，重启进程一次都修不好，只会把现场清掉——重启后计数器
#   归零，下一次失败要再等几分钟才重新累积，而故障原因（比如磁盘满了）在
#   journal 里的线索反而变稀了。真正需要重启的是"进程活着但不响应"，那一类
#   用 --restart-after 显式打开即可。
#
# 退出码即健康状态，便于 systemd 与监控直接消费：
#   0 健康（/healthz 200 且 status=ok）
#   1 不可达（连不上、超时）
#   2 degraded（服务响应了，但 /healthz 返回 503）
#
# 用法：
#   hopdrop-healthcheck                      # 记一次状态
#   hopdrop-healthcheck --restart-after 3    # 连续失败 3 次后重启 relay
#   hopdrop-healthcheck -v                   # 把 /healthz 的响应体也打出来

set -uo pipefail

ENV_FILE="${RELAY_ENV_FILE:-/etc/relay/relay.env}"
STATE_DIR="${RELAY_DATA_DIR:-/var/lib/relay}"
STATE_FILE="${STATE_DIR}/healthcheck.state"
ADDR="${RELAY_ADDR:-127.0.0.1:8080}"
RESTART_AFTER=0
VERBOSE=0

while [ $# -gt 0 ]; do
	case "$1" in
	--restart-after)
		shift
		RESTART_AFTER="${1:-0}"
		;;
	--restart-after=*) RESTART_AFTER="${1#--restart-after=}" ;;
	--addr)
		shift
		ADDR="${1:-}"
		;;
	--addr=*) ADDR="${1#--addr=}" ;;
	--state-file)
		shift
		STATE_FILE="${1:-}"
		;;
	--state-file=*) STATE_FILE="${1#--state-file=}" ;;
	-v | --verbose) VERBOSE=1 ;;
	-h | --help)
		sed -n '2,25p' "$0"
		exit 0
		;;
	*)
		printf '未知参数：%s\n' "$1" >&2
		exit 64
		;;
	esac
	shift
done

# 环境变量文件是配置的唯一真源，优先从它取监听地址，命令行参数次之。
if [ -f "$ENV_FILE" ]; then
	from_env="$(awk -F= '/^RELAY_ADDR=/ {print $2}' "$ENV_FILE" | tail -1)"
	if [ -n "${from_env}" ] && [ "$ADDR" = "127.0.0.1:8080" ]; then
		ADDR="$from_env"
	fi
fi

log() {
	# logger 让输出进 journal 并带 tag，便于 `journalctl -t hopdrop-healthcheck`。
	if command -v logger >/dev/null 2>&1; then
		logger -t hopdrop-healthcheck -p "$1" -- "$2"
	fi
	printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$2"
}

read_state() {
	FAIL_COUNT=0
	FIRST_FAIL_TS=""
	LAST_OK_TS=""
	LAST_STATUS="unknown"
	[ -f "$STATE_FILE" ] || return 0
	# shellcheck disable=SC1090
	while IFS='=' read -r key value; do
		case "$key" in
		fail_count) FAIL_COUNT="${value:-0}" ;;
		first_fail_ts) FIRST_FAIL_TS="${value}" ;;
		last_ok_ts) LAST_OK_TS="${value}" ;;
		last_status) LAST_STATUS="${value}" ;;
		esac
	done <"$STATE_FILE"
	[[ "$FAIL_COUNT" =~ ^[0-9]+$ ]] || FAIL_COUNT=0
}

write_state() {
	local status="$1" fail_count="$2" first_fail="$3" last_ok="$4"
	mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
	{
		printf 'fail_count=%s\n' "$fail_count"
		printf 'first_fail_ts=%s\n' "$first_fail"
		printf 'last_ok_ts=%s\n' "$last_ok"
		printf 'last_status=%s\n' "$status"
		printf 'last_checked_at=%s\n' "$(date +%s)"
	} >"$STATE_FILE" 2>/dev/null || true
}

now="$(date +%s)"
read_state

if ! command -v curl >/dev/null 2>&1; then
	log err "找不到 curl，无法执行健康检查"
	exit 1
fi

# 用 -f 而不是 -f，这一条是整个脚本最关键的地方：
# `-f` 在 503 时只给退出码、**不给响应体**，而 degraded 的原因（reasons）
# 正在响应体里——排查时最需要的那一行恰好被它丢掉了。之前用 -f 的实现要再
# 发一次不带 -f 的请求，还得靠第二个请求去补，多一次往返且容易不同步。
raw="$(curl -sS --fail-with-body --max-time 5 -w '\n%{http_code}' "http://${ADDR}/healthz" 2>/dev/null)"
curl_exit=$?

# curl 的 -w 即使请求失败也会输出，所以最后一行就是 HTTP 状态码；
# 连不上时它是 000。响应体是它上面的部分。
http_code="$(printf '%s\n' "$raw" | tail -n 1)"
body="$(printf '%s\n' "$raw" | sed '$d')"
case "$http_code" in
[0-9][0-9][0-9]) ;;
*) http_code="000" ;;
esac

if [ "$http_code" = "200" ]; then
	FAIL_COUNT=0
	write_state "ok" 0 "" "$now"
	LAST_STATUS="ok"
	if [ "$VERBOSE" = 1 ] && [ -n "$body" ]; then
		status_line="$(printf '%s' "$body" | awk '/"status"/ {print; exit}')"
		log info "healthy ${status_line:-status=ok}"
	else
		log info "healthy"
	fi
	exit 0
fi

# 走到这里就是不健康。区分"服务没响应"和"服务响应说我不健康"——两者的
# 处置方式完全不同，日志里必须能一眼分开。
FAIL_COUNT=$((FAIL_COUNT + 1))
[ -n "$FIRST_FAIL_TS" ] || FIRST_FAIL_TS="$now"

if [ "$http_code" = "503" ]; then
	reasons="$(printf '%s' "$body" | tr -d '\n' | sed -n 's/.*"reasons"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p')"
	log err "degraded（第 ${FAIL_COUNT} 次）httpCode=503 reasons=[${reasons}]"
	write_state "degraded" "$FAIL_COUNT" "$FIRST_FAIL_TS" "$LAST_OK_TS"
	EXIT_CODE=2
else
	log err "不可达（第 ${FAIL_COUNT} 次）addr=${ADDR} curlExit=${curl_exit} httpCode=${http_code}"
	write_state "unreachable" "$FAIL_COUNT" "$FIRST_FAIL_TS" "$LAST_OK_TS"
	EXIT_CODE=1
fi

if [ "$RESTART_AFTER" -gt 0 ] && [ "$FAIL_COUNT" -ge "$RESTART_AFTER" ]; then
	# 只有"不可达"才重启。degraded 是服务自己报告的状态，重启它没有意义
	# （见文件顶部说明）。
	if [ "$EXIT_CODE" = 1 ]; then
		log warning "连续 ${FAIL_COUNT} 次不可达，重启 relay.service"
		if command -v systemctl >/dev/null 2>&1; then
			systemctl restart relay.service && log info "已重启 relay.service" ||
				log err "重启 relay.service 失败"
		fi
		# 重启后清零，避免在没有修复的情况下每 3 分钟一次无限重启。
		write_state "restarted" 0 "" "$LAST_OK_TS"
	else
		log warning "状态为 degraded，按设计不自动重启；请人工处理磁盘或配额问题"
	fi
fi

exit "$EXIT_CODE"
