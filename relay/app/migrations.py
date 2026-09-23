"""顺序 SQL 迁移。

方案 8 节：`migrations/` 目录下顺序编号的 `.sql` 文件，由启动时读取
`schema_version` 表决定执行到哪一版。不使用 Alembic。

每个迁移连同它的版本号写入在**同一个事务**里完成，所以不存在
"DDL 跑了一半但版本号没记上"的中间态。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .db import Database

logger = logging.getLogger("relay.migrations")

FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
)
"""


class MigrationError(RuntimeError):
    """迁移文件命名非法、重复，或执行失败。"""


def discover(migrations_dir: Path) -> list[tuple[int, Path]]:
    """扫描迁移目录，返回按版本号升序的 (version, path)。"""
    if not migrations_dir.is_dir():
        raise MigrationError(f"迁移目录不存在：{migrations_dir}")

    found: dict[int, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(f"迁移文件名必须是 NNNN_名称.sql，当前为：{path.name}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"迁移版本号重复：{version}（{found[version].name} 与 {path.name}）")
        found[version] = path

    versions = sorted(found)
    for previous, current in zip(versions, versions[1:]):
        if current != previous + 1:
            logger.warning("迁移版本号不连续：%04d 之后是 %04d", previous, current)

    return [(version, found[version]) for version in versions]


async def current_version(db: Database) -> int:
    row = await db.fetchone("SELECT MAX(version) AS version FROM schema_version")
    if row is None or row["version"] is None:
        return 0
    return int(row["version"])


async def apply_migrations(db: Database, migrations_dir: Path) -> list[int]:
    """执行所有未应用的迁移，返回本次应用的版本号列表。"""
    pending = discover(Path(migrations_dir))

    async with db.write() as conn:
        await conn.execute(SCHEMA_VERSION_DDL)

    rows = await db.fetchall("SELECT version FROM schema_version")
    applied = {int(row["version"]) for row in rows}

    done: list[int] = []
    for version, path in pending:
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        now = int(time.time())
        # BEGIN / COMMIT 写进脚本本身：executescript 不参与外层事务。
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            f"INSERT INTO schema_version (version, applied_at) VALUES ({version}, {now});\n"
            "COMMIT;\n"
        )
        try:
            await db.conn.executescript(script)
        except Exception as exc:  # noqa: BLE001 - 原样上抛，带上文件名才有排查价值
            raise MigrationError(f"迁移 {path.name} 执行失败：{exc}") from exc
        done.append(version)
        logger.info("已应用迁移 %s", path.name)

    return done
