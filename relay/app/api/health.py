"""健康检查与磁盘用量。

方案 14.5：以下任一情况 `status` 返回 `degraded` 且 HTTP 状态码为 503——
数据库不可写、数据目录不可写、磁盘剩余低于预留值、relay 数据目录超过配额、
或清理任务超过 3 小时未成功。

注意与 7.1 的阈值是两套路标，不要混：
- healthz 用的是**硬线**：磁盘剩余 < 预留值，或数据目录 > 配额，即视为不健康；
- 上传接口用的是**软线**：磁盘剩余 < 4 GB 或数据目录 > 2.4 GB 就开始告警，
  更接近硬线时直接拒绝新上传（M8 实现）。文本、下载、删除不受影响。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Config
from ..db import Database
from ..state import RuntimeState

router = APIRouter()

# 清理任务超过这个时长没成功，就认为处于不健康状态（方案 14.5）。
CLEANUP_STALE_SEC = 3 * 3600


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            # 文件可能正好被清理任务删掉。用量统计不是关键路径，跳过即可。
            continue
    return total


def _dir_writable(path: Path) -> bool:
    probe = path / ".healthz-probe"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False
    return True


async def _collect(db: Database, cfg: Config, state: RuntimeState) -> dict[str, object]:
    database_ok = True
    try:
        await db.check_writable()
    except Exception:  # noqa: BLE001 - 健康检查不该因为自身抛错而 500
        database_ok = False

    usage = await asyncio.to_thread(shutil.disk_usage, str(cfg.data_dir))
    dir_bytes = await asyncio.to_thread(_dir_size_bytes, cfg.data_dir)
    dir_ok = await asyncio.to_thread(_dir_writable, cfg.data_dir)

    reasons: list[str] = []
    if not database_ok:
        reasons.append("database_not_writable")
    if not dir_ok:
        reasons.append("data_dir_not_writable")
    if usage.free < cfg.disk_reserve_bytes:
        reasons.append("disk_free_below_reserve")
    if dir_bytes > cfg.data_quota_bytes:
        reasons.append("data_dir_over_quota")
    if state.last_cleanup_at is not None and int(time.time()) - state.last_cleanup_at > CLEANUP_STALE_SEC:
        reasons.append("cleanup_stale")

    return {
        "status": "degraded" if reasons else "ok",
        "database": "ok" if database_ok else "error",
        "diskFreeBytes": int(usage.free),
        "dataDirBytes": int(dir_bytes),
        "dataDirQuotaBytes": int(cfg.data_quota_bytes),
        "lastCleanupAt": state.last_cleanup_at,
        "websocketConnections": state.websocket_connections,
        "version": __version__,
        "uptimeSeconds": int(time.time()) - state.started_at if state.started_at else 0,
        "reasons": reasons,
    }


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    cfg: Config = request.app.state.config
    db: Database = request.app.state.db
    state: RuntimeState = request.app.state.runtime

    payload = await _collect(db, cfg, state)
    status_code = 200 if payload["status"] == "ok" else 503
    return JSONResponse(status_code=status_code, content=payload)
