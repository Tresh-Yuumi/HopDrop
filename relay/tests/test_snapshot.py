"""快照（`GET /api/snapshot`）。

这个接口是客户端一致性的唯一兜底，所以测试的重点不是字段齐不齐，而是
**它和分页接口说的是不是同一件事**：可见性、排序、已删除消息的处理，如果
两边不一致，客户端重拉一次快照反而会把界面改错。
"""

from __future__ import annotations

import time
import uuid

from conftest import RelayEnv


def _create_board(client, name: str, retention: int = 0) -> dict:
    response = client.post("/api/boards", json={"name": name, "retention": retention})
    assert response.status_code == 201, response.text
    return response.json()["board"]


def _create_note(client, board_id: str, content: str) -> dict:
    # mutationId 只允许字母数字与连字符下划线（notes.MUTATION_ID_ALLOWED），
    # 所以不能拿中文正文拼一个——用 uuid4 的十六进制形式，正好在允许集内。
    response = client.post(
        f"/api/boards/{board_id}/notes",
        json={"content": content, "mutationId": uuid.uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()["note"]


def _spread_created_at(relay_env: RelayEnv) -> None:
    """把库里所有消息的 `created_at` 按插入顺序拉开一秒。

    `created_at` 只有秒级精度（方案 9.1），排序键是 `(created_at DESC, id DESC)`，
    而 id 是随机的 uuid——同一秒内写入的多条消息之间，谁算"更近"完全由随机
    后缀决定。任何关于"最近 N 条是哪几条"的断言，都必须先把时间拉开，否则
    断言的是一个随机结果（这个测试第一次写就踩到了）。
    """
    rows = relay_env.read("SELECT id FROM notes ORDER BY rowid ASC")
    base = int(time.time()) - len(rows)
    for index, row in enumerate(rows):
        relay_env.execute(
            "UPDATE notes SET created_at = ? WHERE id = ?",
            (base + index, row["id"]),
        )


def test_snapshot_requires_session(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/api/snapshot")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_snapshot_contains_initial_boards_and_rev(relay_env: RelayEnv) -> None:
    """新建房间就有两个区域，rev 从 0 开始。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/api/snapshot")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == "owner"
    assert body["rev"] == 0
    assert [board["name"] for board in body["boards"]] == ["日常", "访客区"]
    assert [board["isGuest"] for board in body["boards"]] == [False, True]
    assert all(board["notes"] == [] for board in body["boards"])
    assert body["room"]["name"] == "测试房间"
    assert body["device"]["id"]
    # M7 之前恒为空，但键必须在——前端按最终形状写渲染逻辑。
    assert body["files"] == []


def test_snapshot_rev_matches_database(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "一")
        _create_note(client, board["id"], "二")
        response = client.get("/api/snapshot")

    body = response.json()
    # 建区 1 次 + 两条消息 2 次。
    assert body["rev"] == 3
    assert body["rev"] == relay_env.room_rev()


def test_snapshot_notes_match_paged_endpoint(relay_env: RelayEnv) -> None:
    """同一个区域，快照与分页接口给出的消息顺序与内容必须一致。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "第一条")
        _create_note(client, board["id"], "第二条")
        _create_note(client, board["id"], "第三条")
    _spread_created_at(relay_env)

    with relay_env.paired_client() as client:
        snapshot = client.get("/api/snapshot").json()
        paged = client.get(f"/api/boards/{board['id']}/notes").json()

    from_snapshot = next(b for b in snapshot["boards"] if b["id"] == board["id"])
    assert [n["content"] for n in from_snapshot["notes"]] == ["第三条", "第二条", "第一条"]
    assert [n["id"] for n in from_snapshot["notes"]] == [
        n["id"] for n in paged["notes"]
    ]


def test_snapshot_truncates_notes_but_keeps_total_count(relay_env: RelayEnv) -> None:
    """每区只给最近 N 条，但 `noteCount` 是真实总数。

    这两者的区别决定前端能不能显示"还有更多"——如果 `noteCount` 也跟着被
    截断，界面上会显示"3 条"而实际有 6 条，且没有任何办法察觉。
    """
    relay_env.add_room()
    with relay_env.paired_client(RELAY_SNAPSHOT_NOTE_LIMIT="2") as client:
        board = _create_board(client, "工作")
        for index in range(5):
            _create_note(client, board["id"], f"第{index}条")

    _spread_created_at(relay_env)

    with relay_env.paired_client(RELAY_SNAPSHOT_NOTE_LIMIT="2") as client:
        body = client.get("/api/snapshot").json()

    from_snapshot = next(b for b in body["boards"] if b["id"] == board["id"])
    assert len(from_snapshot["notes"]) == 2
    assert [n["content"] for n in from_snapshot["notes"]] == ["第4条", "第3条"]
    assert from_snapshot["noteCount"] == 5


def test_snapshot_excludes_deleted_notes(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        kept = _create_note(client, board["id"], "留着")
        removed = _create_note(client, board["id"], "删掉")
        assert client.delete(f"/api/notes/{removed['id']}").status_code == 204

        body = client.get("/api/snapshot").json()

    from_snapshot = next(b for b in body["boards"] if b["id"] == board["id"])
    assert [n["id"] for n in from_snapshot["notes"]] == [kept["id"]]
    assert from_snapshot["noteCount"] == 1


def test_snapshot_excludes_archived_boards(relay_env: RelayEnv) -> None:
    """归档区不进快照（与 `/api/boards` 默认行为一致）。

    直接改库摆状态：归档接口属于 M9，但"归档了就该从主界面消失"这件事在
    M4 就必须成立——否则清理任务把区域归档后，前端还会一直显示它。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "会归档的")

    relay_env.execute(
        "UPDATE boards SET status = 'archived', archived_at = ? WHERE id = ?",
        (int(time.time()), board["id"]),
    )

    with relay_env.paired_client() as client:
        body = client.get("/api/snapshot").json()

    assert board["id"] not in [item["id"] for item in body["boards"]]


def test_guest_snapshot_only_sees_guest_board(relay_env: RelayEnv) -> None:
    """访客的快照里只有访客区（方案 16.1）。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "主人的工作区")
        _create_note(client, board["id"], "主人写的内容")

    guest = relay_env.add_guest_device()
    with relay_env.cookie_client(guest.token) as client:
        response = client.get("/api/snapshot")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == "guest"
    assert [item["name"] for item in body["boards"]] == ["访客区"]
    assert board["id"] not in [item["id"] for item in body["boards"]]


def test_snapshot_does_not_leak_other_rooms(relay_env: RelayEnv) -> None:
    """另一个房间的区域与消息一个字都不该出现。"""
    first = relay_env.add_room("一号房间")
    second = relay_env.add_room("二号房间")

    with relay_env.paired_client() as client:
        body = client.get("/api/snapshot").json()
    assert body["room"]["id"] == first.room_id

    # 换一个 client 会拿到新的 app 实例，但 cookie 还在旧 client 里；
    # 这里直接用第二个房间的主人 secret 配对，验证快照完全属于第二个房间。
    with relay_env.client() as client:
        assert client.get(f"/pair/{second.owner_secret}").status_code == 303
        other = client.get("/api/snapshot").json()

    assert other["room"]["id"] == second.room_id
    assert other["rev"] == 0
    assert {item["id"] for item in other["boards"]}.isdisjoint(
        {item["id"] for item in body["boards"]}
    )


def test_snapshot_notes_are_ordered_newest_first(relay_env: RelayEnv) -> None:
    """同一秒内写入的多条消息也要有稳定顺序（次级键是 id）。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        created = [_create_note(client, board["id"], f"n{index}") for index in range(6)]
        body = client.get("/api/snapshot").json()

    from_snapshot = next(b for b in body["boards"] if b["id"] == board["id"])
    # 断言的是"排序键降序"而不是"id 降序"：created_at 只有秒级精度，一旦
    # 这六条跨了秒，两者就不等价了。排序键就是 (createdAt, id)。
    keys = [(n["createdAt"], n["id"]) for n in from_snapshot["notes"]]
    assert keys == sorted(keys, reverse=True)
    assert {n["id"] for n in from_snapshot["notes"]} == {note["id"] for note in created}
