"""本地开发入口：`python -m app`。

刻意不提供 `--workers`。方案 2.2 第 1 条：多 worker 会把 WebSocket 连接表
和全局写锁分裂成多份，一致性直接失效，而且症状很隐蔽。
"""

from __future__ import annotations

import uvicorn

from .config import load_config
from .main import app


def main() -> None:
    cfg = load_config()
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
