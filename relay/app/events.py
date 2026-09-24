"""事务提交后要广播的变更。

单独一个模块，是为了让 `db.py`（产生事件）与 `realtime.py`（消费事件）
不必互相导入：前者是数据层，后者是传输层，谁也不该依赖谁。

事件名集中成常量，是因为它们同时出现在服务端构造（业务层）和客户端匹配
（前端）两侧。散落的字符串字面量一旦拼错，症状是"界面偶尔少更新一次"，
极难定位；常量至少让服务端这一侧不会写错。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EVENT_BOARD_CREATED = "board.created"
EVENT_BOARD_UPDATED = "board.updated"
EVENT_NOTE_CREATED = "note.created"
EVENT_NOTE_UPDATED = "note.updated"
EVENT_NOTE_DELETED = "note.deleted"


@dataclass(frozen=True)
class RoomEvent:
    """一次已提交、且必须让客户端知道的房间变更。

    `rev` 是这次写入**之后** `rooms.rev` 的值，不是"第几次变更"。客户端
    拿它与本地 rev 比较：正好 +1 就按 `payload` 增量应用，否则说明中间漏了
    推送，改拉一次快照。

    `payload` 里放的是已经序列化好的对外形状（`serialize_board` /
    `serialize_note` 的产物）。这样广播和 HTTP 响应共用同一份序列化逻辑，
    两边不会出现"接口返回的字段和推送里的不一样"。
    """

    room_id: str
    rev: int
    event: str
    payload: dict[str, Any]
