"""进程内运行时状态。

只放"进程活着期间才有意义"的东西。事实来源永远是 SQLite，这里的东西
丢了不影响正确性——重启后重新建立即可。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RuntimeState:
    started_at: int = 0
    # 由 WebSocket 层维护（M4）
    websocket_connections: int = 0
    # 由清理任务维护（M8）。None 表示清理任务尚未接入。
    last_cleanup_at: int | None = None
    last_cleanup_ok: bool | None = None
