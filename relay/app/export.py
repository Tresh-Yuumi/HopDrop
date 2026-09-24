"""区域导出成 `.txt` / `.md`（方案 4.1、9.3）。

按需现生成，不留缓存文件——导出的内容永远等于**此刻**库里的内容，不存在
"导出的是三天前那份"的可能。

---

**三处需要写清楚的取定。**

一、**顺序是时间正序**，与接口和快照相反。分页与快照倒序是因为界面从上往下
是"最新在前"；导出的阅读顺序是"从头读起"，所以正序。两个方向各自成立，不要
为了"统一"去改其中一个。

二、**排序键必须带次级键兜底**。`created_at` 只有秒级精度，同一秒写入的两条
消息时间戳相同，只按时间排的话它们的相对顺序由 SQLite 自行决定——导出两次
可能得到不同顺序。次级键用 `rowid` 而不是 `id`：`id` 是随机 hex，同一秒内的
几条会按"谁的 id 大"排列，导出文件可能从中间读起。理由与取值见
`notes.NOTE_ORDER_ASC`。

三、**已软删除的消息不导出**。它们已经进了回收站（方案 4.1），不是区域内容
的一部分。回收站里恢复之后再导出，它才会出现。

**一条已知的格式限制**：`.md` 的正文原样输出，不做转义。若某条消息正文本身
长得像 Markdown（例如以 `### ` 开头），它在 `.md` 里会被渲染成标题。需要与
原始内容逐字一致时用 `.txt`——`.txt` 除了统一的头部与分隔行之外是忠实副本。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import quote

from .boards import load_board
from .db import Database
from .errors import AppError
from .notes import NOTE_COLUMNS, NOTE_FROM, NOTE_ORDER_ASC

EXPORT_FORMATS = ("txt", "md")

# 一次导出的消息条数上限。
#
# 方案没有规定，而 `RELAY_BOARD_NOTE_LIMIT`（单区域消息上限）目前**并没有
# 被任何写入路径强制执行**——那是 M8 的配额工作。所以导出不能假设"区域里
# 最多两万条"这个前提成立：没有上限的话，一个攒了几十万条的区域被导出时会
# 让服务端把整个结果拼成一个大字符串。这里显式兜住，并在超出时**在文件头
# 里写明截断了**，而不是安静地少给内容。
EXPORT_MAX_NOTES = 20000

# Windows 与多数文件系统不接受这些字符，Linux 只禁 `/`。统一替换成下划线，
# 使文件名在四个客户端平台上都能落盘。
_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_FILENAME_MAX = 80

_MEDIA_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}


@dataclass(frozen=True)
class BoardExport:
    """一次导出的产物。文件名与响应头一起给出，调用方不需要再拼。"""

    filename: str
    content: str
    media_type: str
    truncated: bool

    def content_disposition(self) -> str:
        """RFC 5987 形式的 Content-Disposition。

        中文区域名不能直接放进 `filename="..."`——那个位置按 RFC 只能放
        ASCII，浏览器对非 ASCII 的处理各不相同（有的丢字符，有的整个忽略）。
        所以给两份：`filename=` 放纯 ASCII 兜底名，`filename*=UTF-8''...`
        放真正的名字，认识后者的浏览器（现在全部主流浏览器）用它。
        """
        ascii_name = f"hopdrop-export.{self.filename.rsplit('.', 1)[-1]}"
        return (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(self.filename, safe='')}"
        )


def normalize_format(raw: object) -> str:
    if raw is None:
        return "txt"
    if not isinstance(raw, str) or raw not in EXPORT_FORMATS:
        raise AppError.bad_request(
            f"format 只能是 {'/'.join(EXPORT_FORMATS)}", "invalid_export_format"
        )
    return raw


def safe_filename_stem(name: str) -> str:
    """把区域名变成可落盘的文件名主干。"""
    cleaned = _FILENAME_UNSAFE.sub("_", name).strip().strip(".")
    cleaned = cleaned[:_FILENAME_MAX].strip()
    return cleaned or "board"


def _stamp(value: int) -> str:
    """把 Unix 秒格式化成导出文件里的时间。

    用**服务端本地时区**（`time.localtime`）。方案 14.1 的系统初始化里有一
    条 `timedatectl set-timezone Asia/Shanghai`，所以服务端本地时间就是
    北京时间。这里刻意不引入时区配置项：多一个可以配错的东西，就多一种
    "导出的时间对不上"的可能。
    """
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _retention_text(retention: int) -> str:
    return "永久" if retention == 0 else f"{retention} 天"


def _note_meta(row: dict, *, fmt: str) -> str:
    """一条消息的元信息行（时间 + 作者 + 标记）。"""
    author = row["author_name"] or "已离开的设备"
    marks: list[str] = []
    if row["pinned"]:
        marks.append("置顶")
    if int(row["updated_at"]) > int(row["created_at"]):
        marks.append("已编辑")
    if row["kind"] != "text":
        # 文件消息（M7）。正文列对文件消息存的是占位说明，这里明确标出，
        # 免得读导出的人把占位文本当成用户写的内容。
        marks.append("文件消息")
    suffix = f"（{'，'.join(marks)}）" if marks else ""
    if fmt == "md":
        return f"### {_stamp(int(row['created_at']))} · {author}{suffix}"
    return f"[{_stamp(int(row['created_at']))}] {author}{suffix}"


def _body_of(row: dict) -> str:
    if row["kind"] != "text":
        # 文件在导出里不展开（方案 6 的下载是独立接口）。这里给一句说明而
        # 不是空行——空行会让人以为消息是空的。
        return "（文件消息，请到应用页查看）"
    return row["content"]


async def build_board_export(
    db: Database,
    *,
    room_id: str,
    board_id: str,
    role: str,
    fmt: str,
    now: int | None = None,
) -> BoardExport:
    """生成导出内容。可见性与可导出的状态都沿用 `load_board` 的判定。

    归档区**可以导出**（方案 4.2：归档区只能导出和恢复），所以这里不看
    `status`——`load_board` 本身也不看，它只管"这个区域对这双眼睛是否可见"。
    访客导主人的区域会被 `load_board` 挡成 404。
    """
    now = int(time.time()) if now is None else now
    resolved = normalize_format(fmt)

    board = await load_board(db, room_id=room_id, board_id=board_id, role=role)

    rows = await db.fetchall(
        f"SELECT {NOTE_COLUMNS} {NOTE_FROM}"
        " WHERE n.board_id = ? AND n.room_id = ? AND n.deleted_at IS NULL"
        f" ORDER BY {NOTE_ORDER_ASC}"
        " LIMIT ?",
        (board_id, room_id, EXPORT_MAX_NOTES + 1),
    )
    truncated = len(rows) > EXPORT_MAX_NOTES
    rows = rows[:EXPORT_MAX_NOTES]

    stem = safe_filename_stem(board["name"])
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))

    if resolved == "md":
        content = _render_markdown(board, rows, now=now, truncated=truncated)
    else:
        content = _render_text(board, rows, now=now, truncated=truncated)

    return BoardExport(
        filename=f"{stem}-{stamp}.{resolved}",
        content=content,
        media_type=_MEDIA_TYPES[resolved],
        truncated=truncated,
    )


def _render_text(board: dict, rows: list[dict], *, now: int, truncated: bool) -> str:
    lines = [
        "HopDrop 区域导出",
        f"区域：{board['name']}",
        f"保留期：{_retention_text(int(board['retention']))}",
        f"消息数：{len(rows)}",
        f"导出时间：{_stamp(now)}",
    ]
    if truncated:
        lines.append(
            f"注意：本区域消息超过 {EXPORT_MAX_NOTES} 条，此文件只包含最早的"
            f" {EXPORT_MAX_NOTES} 条。"
        )
    lines.append("")
    lines.append("─" * 40)

    for row in rows:
        lines.append("")
        lines.append(_note_meta(row, fmt="txt"))
        lines.append(_body_of(row))

    lines.append("")
    return "\n".join(lines)


def _render_markdown(board: dict, rows: list[dict], *, now: int, truncated: bool) -> str:
    lines = [
        f"# {board['name']}",
        "",
        f"- 保留期：{_retention_text(int(board['retention']))}",
        f"- 消息数：{len(rows)}",
        f"- 导出时间：{_stamp(now)}",
    ]
    if truncated:
        lines.append(
            f"- ⚠️ 消息超过 {EXPORT_MAX_NOTES} 条，此文件只包含最早的 {EXPORT_MAX_NOTES} 条"
        )
    lines.append("")
    lines.append("---")

    for row in rows:
        lines.append("")
        lines.append(_note_meta(row, fmt="md"))
        lines.append("")
        lines.append(_body_of(row))

    lines.append("")
    return "\n".join(lines)
