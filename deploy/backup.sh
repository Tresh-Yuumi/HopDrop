#!/usr/bin/env bash
# HopDrop 数据库备份（方案 14.6 / 7.5）
#
# 只备份 SQLite。本产品不承诺文件备份——文件 7 天过期后由用户重新上传。
# 归档区数据始终在数据库里，所以备份数据库即备份了归档。
#
# 安装：
#   cp deploy/backup.sh /usr/local/bin/hopdrop-backup && chmod 755 /usr/local/bin/hopdrop-backup
#   echo '0 3 * * * root /usr/local/bin/hopdrop-backup' > /etc/cron.d/hopdrop-backup
#
# 依赖 sqlite3 CLI（方案 14.1 的系统初始化里要装上）。
# 用 CLI 而不是 Python，是因为恢复流程本身要用它做 PRAGMA integrity_check
# 和人工查看，装一个是净收益。

set -eu

DATA_DIR="${RELAY_DATA_DIR:-/var/lib/relay}"
DB="${DATA_DIR}/relay.db"
BACKUP_DIR="${DATA_DIR}/backup"
KEEP=7
MIN_FREE_KB=$((512 * 1024))

if [ ! -f "${DB}" ]; then
	echo "backup failed: ${DB} not found" >&2
	exit 1
fi

mkdir -p "${BACKUP_DIR}"

# 磁盘不足时跳过本次备份，且不覆盖任何旧备份（方案 7.5）。
free_kb="$(df -Pk "${DATA_DIR}" | awk 'NR==2 {print $4}')"
if [ "${free_kb}" -lt "${MIN_FREE_KB}" ]; then
	echo "skip backup: free space ${free_kb} KiB is under 512 MiB" >&2
	exit 0
fi

stamp="$(date +%Y%m%d-%H%M%S)"
target="${BACKUP_DIR}/relay-${stamp}.db"
staging="${target}.part"

# 每次执行生成独立文件名（含时分秒），所以重复运行不会覆盖当天已有的备份。
rm -f "${staging}"
trap 'rm -f "${staging}"' EXIT

sqlite3 "${DB}" "PRAGMA wal_checkpoint(PASSIVE); VACUUM INTO '${staging}';"
mv "${staging}" "${target}"
chmod 600 "${target}"

# 保留最近 KEEP 份
ls -1t "${BACKUP_DIR}"/relay-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
	rm -f "${old}"
done

echo "backup ok: ${target}"
