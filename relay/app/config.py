"""配置加载。

所有可调参数都来自 RELAY_* 环境变量，没有配置文件。这一层的原则是：
**非法配置必须在启动时立刻失败**，不要留到运行时才炸。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

GIB = 1024 ** 3

# DDL 里 files.size 的 CHECK 约束是 1..20971520。上限写死在这里，
# 否则调大配置后上传会在写库阶段才失败，报错信息很难懂。
MAX_FILE_BYTES_HARD_LIMIT = 20 * 1024 * 1024


class ConfigError(RuntimeError):
    """配置项缺失或取值非法。"""


def _raw(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    text = _raw(env, name)
    if text is None:
        return default
    try:
        value = int(text)
    except ValueError:
        raise ConfigError(f"{name} 必须是整数，当前值：{text!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，当前值：{value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} 不能大于 {maximum}，当前值：{value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, *, minimum: float | None = None) -> float:
    text = _raw(env, name)
    if text is None:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{name} 必须是数字，当前值：{text!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，当前值：{value}")
    return value


@dataclass(frozen=True)
class Config:
    # 监听地址
    host: str
    port: int
    # 数据目录
    data_dir: Path
    # 配额与寿命
    max_file_bytes: int
    file_ttl_days: int
    archive_ttl_days: int
    room_file_quota_bytes: int
    disk_reserve_bytes: int
    data_quota_bytes: int
    guest_ttl_min: int
    # 容量与分页
    cleanup_interval_sec: int
    snapshot_note_limit: int
    page_size_default: int
    page_size_max: int
    note_max_bytes: int
    board_note_limit: int
    # 原始值，仅用于日志展示
    raw_disk_reserve_gb: float = field(default=3.0, repr=False)
    raw_data_quota_gb: float = field(default=3.0, repr=False)
    raw_room_file_quota_gb: float = field(default=2.0, repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "relay.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.files_dir, self.thumbs_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict[str, Any]:
        """用于启动日志。目前没有密钥类字段，仍然集中在这里以便将来脱敏。"""
        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "max_file_bytes": self.max_file_bytes,
            "file_ttl_days": self.file_ttl_days,
            "archive_ttl_days": self.archive_ttl_days,
            "room_file_quota_bytes": self.room_file_quota_bytes,
            "disk_reserve_bytes": self.disk_reserve_bytes,
            "data_quota_bytes": self.data_quota_bytes,
            "guest_ttl_min": self.guest_ttl_min,
            "cleanup_interval_sec": self.cleanup_interval_sec,
            "snapshot_note_limit": self.snapshot_note_limit,
            "page_size_default": self.page_size_default,
            "page_size_max": self.page_size_max,
        }


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    addr = _raw(env, "RELAY_ADDR") or "127.0.0.1:8080"
    if ":" not in addr:
        raise ConfigError(f"RELAY_ADDR 必须形如 host:port，当前值：{addr!r}")
    host, _, port_text = addr.rpartition(":")
    if not host:
        host = "127.0.0.1"
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(f"RELAY_ADDR 的端口不是数字，当前值：{addr!r}") from None
    if not (1 <= port <= 65535):
        raise ConfigError(f"RELAY_ADDR 的端口超出范围，当前值：{port}")

    # 默认值面向本地开发；生产由 /etc/relay/relay.env 覆盖。
    data_dir = Path(_raw(env, "RELAY_DATA_DIR") or "./data").expanduser()

    max_file_bytes = _int(
        env,
        "RELAY_MAX_FILE_BYTES",
        MAX_FILE_BYTES_HARD_LIMIT,
        minimum=1,
        maximum=MAX_FILE_BYTES_HARD_LIMIT,
    )
    disk_reserve_gb = _float(env, "RELAY_DISK_RESERVE_GB", 3.0, minimum=0.0)
    data_quota_gb = _float(env, "RELAY_DATA_QUOTA_GB", 3.0, minimum=0.1)
    room_file_quota_gb = _float(env, "RELAY_ROOM_FILE_QUOTA_GB", 2.0, minimum=0.1)

    return Config(
        host=host,
        port=port,
        data_dir=data_dir,
        max_file_bytes=max_file_bytes,
        file_ttl_days=_int(env, "RELAY_FILE_TTL_DAYS", 7, minimum=1, maximum=3650),
        archive_ttl_days=_int(env, "RELAY_ARCHIVE_TTL_DAYS", 90, minimum=1, maximum=3650),
        room_file_quota_bytes=int(room_file_quota_gb * GIB),
        disk_reserve_bytes=int(disk_reserve_gb * GIB),
        data_quota_bytes=int(data_quota_gb * GIB),
        guest_ttl_min=_int(env, "RELAY_GUEST_TTL_MIN", 120, minimum=5, maximum=1440),
        cleanup_interval_sec=_int(env, "RELAY_CLEANUP_INTERVAL_SEC", 3600, minimum=60),
        snapshot_note_limit=_int(env, "RELAY_SNAPSHOT_NOTE_LIMIT", 100, minimum=1, maximum=500),
        page_size_default=_int(env, "RELAY_PAGE_SIZE_DEFAULT", 50, minimum=1, maximum=500),
        page_size_max=_int(env, "RELAY_PAGE_SIZE_MAX", 200, minimum=1, maximum=1000),
        note_max_bytes=_int(env, "RELAY_NOTE_MAX_BYTES", 64 * 1024, minimum=256),
        board_note_limit=_int(env, "RELAY_BOARD_NOTE_LIMIT", 20000, minimum=100),
        raw_disk_reserve_gb=disk_reserve_gb,
        raw_data_quota_gb=data_quota_gb,
        raw_room_file_quota_gb=room_file_quota_gb,
    )
