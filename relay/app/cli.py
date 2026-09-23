"""命令行管理入口。

```bash
python -m app.cli init [--name 我的空间]   # 首次创建房间，打印主人链接
python -m app.cli rotate [--room-id ID]   # 重置主人链接（链接丢失时的找回路径）
```

为什么必须有这个入口：产品**没有注册接口**（方案 3.1 的房间是运维动作，
不是用户动作），而主人 secret 在库里只有哈希。这意味着一旦链接丢失且所有
已配对设备都被撤销，就再也没有任何途径产生新链接——只能进数据库手改。
`rotate` 就是补上这个洞。

两个命令都先跑迁移再动作，所以第一次部署时可以只执行 `init` 而不必先手动
启动一次服务。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import Config, load_config
from .db import Database
from .migrations import apply_migrations
from .rooms import (
    DEFAULT_ROOM_NAME,
    count_rooms,
    create_room,
    list_rooms,
    rotate_owner_secret,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _enable_utf8_output() -> None:
    """Windows 控制台默认是 GBK，直接打印中文会抛 UnicodeEncodeError。

    不处理的话，正是最需要看到输出的那两个命令会挂掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass


async def _prepare() -> tuple[Config, Database]:
    cfg = load_config()
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    await db.connect()
    await apply_migrations(db, MIGRATIONS_DIR)
    return cfg, db


def _print_pair_hint(cfg: Config, pair_path: str) -> None:
    print()
    print(f"主人链接（路径）：{pair_path}")
    print("完整链接 = 你的域名 + 上面的路径，例如 https://你的域名" + pair_path)
    print()
    print("请离线保存。服务端只保存它的哈希，链接丢失后无法找回，")
    print("只能执行 `python -m app.cli rotate` 生成一条新的。")
    print(f"数据库位置：{cfg.db_path}")


async def _cmd_init(args: argparse.Namespace) -> int:
    cfg, db = await _prepare()
    try:
        existing = await count_rooms(db)
        if existing:
            print(f"已存在 {existing} 个房间，未做任何改动。")
            print("如需重置主人链接，请执行：python -m app.cli rotate")
            return 1

        bootstrap = await create_room(db, name=args.name)
        print(f"已创建房间：{bootstrap.room_name}（id={bootstrap.room_id}）")
        print("已自动创建两个初始区域：日常（永久）、访客区（30 天滑动）")
        _print_pair_hint(cfg, bootstrap.pair_path)
        return 0
    finally:
        await db.close()


async def _cmd_rotate(args: argparse.Namespace) -> int:
    cfg, db = await _prepare()
    try:
        rooms = await list_rooms(db)
        if not rooms:
            print("还没有房间。请先执行：python -m app.cli init")
            return 1

        if args.room_id:
            targets = [room for room in rooms if room["id"] == args.room_id]
            if not targets:
                print(f"找不到房间 id={args.room_id}")
                return 1
            room = targets[0]
        elif len(rooms) == 1:
            room = rooms[0]
        else:
            print(f"存在 {len(rooms)} 个房间，请用 --room-id 指定：")
            for item in rooms:
                print(f"  {item['id']}  {item['name']}")
            return 1

        secret = await rotate_owner_secret(db, room_id=room["id"])
        print(f"已重置房间「{room['name']}」的主人链接。旧链接立即失效。")
        print("已配对设备不受影响，无需重新配对。")
        _print_pair_hint(cfg, f"/pair/{secret}")
        return 0
    finally:
        await db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="HopDrop relay 管理命令")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="创建房间并打印主人链接")
    init.add_argument("--name", default=DEFAULT_ROOM_NAME, help=f"房间名，默认「{DEFAULT_ROOM_NAME}」")
    init.set_defaults(handler=_cmd_init)

    rotate = sub.add_parser("rotate", help="重置主人链接")
    rotate.add_argument("--room-id", default=None, help="房间 id；只有一个房间时可省略")
    rotate.set_defaults(handler=_cmd_rotate)

    return parser


def main(argv: list[str] | None = None) -> int:
    _enable_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
