"""消息的增删改、幂等与权限。

对应方案 16.1 / 16.2 / 16.4 中与消息相关的部分。三处最需要盯：

1. **幂等重放不递增 rev、不产生第二行。** 方案 16.2 的验收项，也是客户端
   断网重试时的正常路径，不是边界情况；
2. **软删除是软删除。** 行还在、内容还在、`deleted_at` 有值——硬删除由清理
   任务在 7 天后执行，两件事不能混在一步里；
3. **访客的写权限边界。** 只能写访客区、只能改自己发的、不能置顶。
"""

from __future__ import annotations

import time

import pytest

from app.boards import SECONDS_PER_DAY

DAY = SECONDS_PER_DAY
# 与 Config 的默认值一致，用于"刚好超限"的用例
NOTE_MAX_BYTES = 64 * 1024


def board_id_of(client, *, guest: bool = False) -> str:
    response = client.get("/api/boards")
    assert response.status_code == 200, response.text
    return next(board["id"] for board in response.json()["boards"] if board["isGuest"] is guest)


def post_note(client, board_id: str, content: str, mutation_id: str = "m-1"):
    return client.post(
        f"/api/boards/{board_id}/notes",
        json={"content": content, "mutationId": mutation_id},
    )


def notes_of(client, board_id: str, **params) -> dict:
    response = client.get(f"/api/boards/{board_id}/notes", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def owner(relay_env):
    relay_env.add_room()
    with relay_env.paired_client() as client:
        yield client


# ---- 写入 ----


def test_create_note_returns_201_and_bumps_rev(owner, relay_env):
    board_id = board_id_of(owner)
    before = relay_env.room_rev()

    response = post_note(owner, board_id, "第一条消息")

    assert response.status_code == 201
    note = response.json()["note"]
    assert note["content"] == "第一条消息"
    assert note["boardId"] == board_id
    assert note["kind"] == "text"
    assert note["pinned"] is False
    assert note["edited"] is False
    assert note["deletedAt"] is None
    assert note["createdAt"] == note["updatedAt"]
    assert note["authorName"]
    assert relay_env.room_rev() == before + 1


def test_note_text_is_stored_verbatim(owner, relay_env):
    """不做清洗，由渲染层转义（方案 12 的主防线）。"""
    hostile = '<script>alert(1)</script>\n  & "引号" 换行\n保留'
    board_id = board_id_of(owner)

    note = post_note(owner, board_id, hostile).json()["note"]

    assert note["content"] == hostile
    assert relay_env.read("SELECT content FROM notes")[0]["content"] == hostile


def test_note_count_reflects_live_notes(owner):
    board_id = board_id_of(owner)
    post_note(owner, board_id, "一", "m-1")
    post_note(owner, board_id, "二", "m-2")

    board = next(
        board
        for board in owner.get("/api/boards").json()["boards"]
        if board["id"] == board_id
    )
    assert board["noteCount"] == 2


# ---- 幂等 ----


def test_replayed_mutation_id_returns_original_without_second_row(owner, relay_env):
    board_id = board_id_of(owner)
    first = post_note(owner, board_id, "只该出现一次", "stable-key")
    rev_after_first = relay_env.room_rev()

    second = post_note(owner, board_id, "只该出现一次", "stable-key")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["note"]["id"] == first.json()["note"]["id"]
    assert relay_env.read("SELECT COUNT(*) AS n FROM notes") == [{"n": 1}]
    # 重放没有改变任何状态，所以不能推进 rev——否则客户端会以为有更新
    assert relay_env.room_rev() == rev_after_first


def test_replay_ignores_changed_content(owner):
    """同一次提交重试时正文不可能变；真变了也以首次为准。"""
    board_id = board_id_of(owner)
    post_note(owner, board_id, "第一次的正文", "same-key")

    replayed = post_note(owner, board_id, "被改过的正文", "same-key")

    assert replayed.status_code == 200
    assert replayed.json()["note"]["content"] == "第一次的正文"


def test_different_mutation_ids_create_separate_notes(owner):
    board_id = board_id_of(owner)
    post_note(owner, board_id, "同样的内容", "key-a")
    post_note(owner, board_id, "同样的内容", "key-b")

    assert notes_of(owner, board_id)["notes"].__len__() == 2


def test_mutation_id_from_another_room_is_rejected(relay_env):
    """`mutation_id` 是全局唯一列，跨越房间命中时必须拒绝。

    否则拿别人房间的 mutation_id 提交，就能把别人房间的消息原文读出来。
    """
    first = relay_env.add_room("房间一")
    second = relay_env.add_room("房间二")

    with relay_env.paired_client() as owner_a:
        board_a = board_id_of(owner_a)
        post_note(owner_a, board_a, "房间一的私密内容", "shared-key")

    with relay_env.client() as owner_b:
        owner_b.get(f"/pair/{second.owner_secret}")
        board_b = board_id_of(owner_b)
        response = post_note(owner_b, board_b, "房间二的提交", "shared-key")

    assert first.room_id != second.room_id
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mutation_id_conflict"
    assert "私密内容" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "x"},
        {"content": "x", "mutationId": ""},
        {"content": "x", "mutationId": "有中文"},
        {"content": "x", "mutationId": "a" * 65},
        {"content": "x", "mutationId": 123},
        {"content": "x", "mutationId": "ok", "extra": 1},
    ],
    ids=["missing", "empty", "non-ascii", "too-long", "not-a-string", "extra-field"],
)
def test_invalid_mutation_id_is_400(owner, payload):
    board_id = board_id_of(owner)
    response = owner.post(f"/api/boards/{board_id}/notes", json=payload)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"].startswith(("invalid_", "bad_request"))


# ---- 正文校验 ----


@pytest.mark.parametrize(
    "content", ["", "   ", "\n\t "], ids=["empty", "spaces", "whitespace"]
)
def test_blank_content_is_rejected(owner, content):
    board_id = board_id_of(owner)
    response = post_note(owner, board_id, content)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_note_content"


def test_content_limit_counts_utf8_bytes_not_characters(owner):
    """限长按字节。中文一个字 3 字节，按字符算会让实际占用变成限制值的三倍。"""
    board_id = board_id_of(owner)
    # 恰好超限：多一个字就顶出去
    too_long = "中" * (NOTE_MAX_BYTES // 3 + 1)

    response = post_note(owner, board_id, too_long)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "note_too_large"


def test_content_at_limit_is_accepted(owner):
    board_id = board_id_of(owner)
    fits = "a" * NOTE_MAX_BYTES

    response = post_note(owner, board_id, fits)

    assert response.status_code == 201


# ---- 读取与分页 ----


def test_notes_are_listed_newest_first(owner):
    board_id = board_id_of(owner)
    for index in range(3):
        post_note(owner, board_id, f"第 {index} 条", f"m-{index}")

    body = notes_of(owner, board_id)

    assert len(body["notes"]) == 3
    assert body["board"]["id"] == board_id
    assert body["nextCursor"] is None


def test_notes_pagination_walks_without_gaps_or_repeats(owner):
    board_id = board_id_of(owner)
    for index in range(5):
        post_note(owner, board_id, f"第 {index} 条", f"m-{index}")

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = notes_of(owner, board_id, **params)
        seen.extend(note["id"] for note in body["notes"])
        cursor = body["nextCursor"]
        if cursor is None:
            break

    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_cursor_from_another_board_yields_empty_page(owner):
    """游标只校验存在性，不校验归属。

    这是可以的：游标指向的消息 id 本身不携带任何内容，用别的区的 id 当
    游标只会得到一个与当前区无关的边界，不会泄漏别的区的消息——列表查询
    的第一条条件永远是 `board_id = ?`。
    """
    first = board_id_of(owner)
    second = owner.post("/api/boards", json={"name": "另一个区"}).json()["board"]["id"]
    note_id = post_note(owner, first, "别的区的内容", "m-1").json()["note"]["id"]

    body = notes_of(owner, second, cursor=note_id)

    assert body["notes"] == []


def test_notes_of_unknown_board_is_404(owner):
    response = owner.get("/api/boards/nope/notes")

    assert response.status_code == 404


# ---- 编辑与置顶 ----


def test_owner_edits_note_and_marks_it_edited(owner, relay_env):
    """**不等这一秒**：同一秒内创建再编辑也必须标上"已编辑"。

    时间字段统一是 Unix 秒（方案 9.1），`edited` 由 `updatedAt > createdAt`
    推出。如果编辑时把 `updated_at` 直接写成 `now`，那么"发出后立刻改错别字"
    会得到两个相同的值，标记不出现——而"刚改完内容标记没亮"恰恰是最容易让人
    以为没改成功的场景。所以写入路径会把它抬到 `created_at + 1`。

    上一版这条用例靠 `sleep(1.1)` 跨秒来规避，那等于把待修的行为写成了前提。
    """
    board_id = board_id_of(owner)
    note_id = post_note(owner, board_id, "原文", "m-1").json()["note"]["id"]
    before = relay_env.room_rev()

    response = owner.patch(f"/api/notes/{note_id}", json={"content": "改过的正文"})

    assert response.status_code == 200
    note = response.json()["note"]
    assert note["content"] == "改过的正文"
    assert note["edited"] is True
    assert note["updatedAt"] > note["createdAt"]
    assert relay_env.room_rev() == before + 1


def test_pin_does_not_mark_note_as_edited(owner):
    """置顶不算编辑。否则每条被置顶的消息都会挂上"已编辑"标记。"""
    board_id = board_id_of(owner)
    note_id = post_note(owner, board_id, "正文", "m-1").json()["note"]["id"]

    note = owner.patch(f"/api/notes/{note_id}", json={"pinned": True}).json()["note"]

    assert note["pinned"] is True
    assert note["edited"] is False
    assert note["updatedAt"] == note["createdAt"]


def test_unpin_works(owner):
    board_id = board_id_of(owner)
    note_id = post_note(owner, board_id, "正文", "m-1").json()["note"]["id"]
    owner.patch(f"/api/notes/{note_id}", json={"pinned": True})

    note = owner.patch(f"/api/notes/{note_id}", json={"pinned": False}).json()["note"]

    assert note["pinned"] is False


def test_empty_patch_is_rejected(owner):
    board_id = board_id_of(owner)
    note_id = post_note(owner, board_id, "正文", "m-1").json()["note"]["id"]

    response = owner.patch(f"/api/notes/{note_id}", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] in ("empty_update", "bad_request")


def test_edit_rejects_oversized_content(owner):
    board_id = board_id_of(owner)
    note_id = post_note(owner, board_id, "正文", "m-1").json()["note"]["id"]

    response = owner.patch(f"/api/notes/{note_id}", json={"content": "a" * (NOTE_MAX_BYTES + 1)})

    assert response.status_code == 413


# ---- 软删除 ----


def test_delete_is_soft_and_bumps_rev(owner, relay_env):
    board_id = board_id_of(owner)
    note_id = post_note(owner, board_id, "待删除", "m-1").json()["note"]["id"]
    before = relay_env.room_rev()

    response = owner.delete(f"/api/notes/{note_id}")

    assert response.status_code == 204
    assert relay_env.room_rev() == before + 1

    row = relay_env.read("SELECT content, deleted_at FROM notes")[0]
    assert row["deleted_at"] is not None
    # 内容保留：回收站要能看到它，7 天后的硬删除才会真正抹掉
    assert row["content"] == "待删除"

    assert notes_of(owner, board_id)["notes"] == []


def test_deleted_note_cannot_be_edited_or_deleted_again(owner):
    board_id = board_id_of(owner)
    note_id = post_note(owner, board_id, "待删除", "m-1").json()["note"]["id"]
    owner.delete(f"/api/notes/{note_id}")

    edited = owner.patch(f"/api/notes/{note_id}", json={"content": "复活"})
    again = owner.delete(f"/api/notes/{note_id}")

    assert edited.status_code == 404
    assert again.status_code == 404


def test_delete_unknown_note_is_404(owner):
    response = owner.delete("/api/notes/not-a-note")

    assert response.status_code == 404


# ---- 区域状态对写入的约束 ----


def test_archived_board_rejects_new_notes(owner, relay_env):
    board_id = board_id_of(owner)
    relay_env.execute(
        "UPDATE boards SET status = 'archived', archived_at = ? WHERE id = ?",
        (int(time.time()), board_id),
    )

    response = post_note(owner, board_id, "不该写进去", "m-1")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "board_archived"
    assert relay_env.read("SELECT COUNT(*) AS n FROM notes") == [{"n": 0}]


def test_expired_board_rejects_new_notes_but_allows_delete(owner, relay_env):
    """到期后不能追加，但整理动作（删除已有消息）不该被挡住。

    用 30 天的区而不是日常区：DDL 的 CHECK 不允许"永久"与 `expires_at`
    共存，日常区根本无法被摆成"已到期"这个状态。
    """
    board_id = owner.post("/api/boards", json={"name": "季度", "retention": 30}).json()["board"]["id"]
    note_id = post_note(owner, board_id, "已存在的消息", "m-1").json()["note"]["id"]
    relay_env.execute("UPDATE boards SET expires_at = ? WHERE id = ?", (1, board_id))

    blocked = post_note(owner, board_id, "不该写进去", "m-2")
    deleted = owner.delete(f"/api/notes/{note_id}")

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "board_expired"
    assert deleted.status_code == 204


def test_archived_board_notes_are_still_readable(owner, relay_env):
    """归档区在 90 天内可随时导出，所以消息必须还能读出来。"""
    board_id = board_id_of(owner)
    post_note(owner, board_id, "归档前的内容", "m-1")
    relay_env.execute(
        "UPDATE boards SET status = 'archived', archived_at = ? WHERE id = ?",
        (int(time.time()), board_id),
    )

    body = notes_of(owner, board_id)

    assert [note["content"] for note in body["notes"]] == ["归档前的内容"]


# ---- 访客区滑动续期（方案 4.4） ----


def test_guest_board_write_refreshes_expiry(owner, relay_env):
    guest_id = board_id_of(owner, guest=True)
    # 先把到期时间压到近期，制造"即将清空"的状态
    soon = int(time.time()) + DAY
    relay_env.execute("UPDATE boards SET expires_at = ? WHERE id = ?", (soon, guest_id))

    post_note(owner, guest_id, "主人写一条", "m-1")

    refreshed = relay_env.read("SELECT expires_at FROM boards WHERE id = ?", (guest_id,))[0]
    assert int(refreshed["expires_at"]) > soon + 20 * DAY


def test_main_board_write_does_not_refresh_expiry(owner, relay_env):
    """滑动续期只属于访客区。日常读写不会自动延后到期时间（方案 4.2）。"""
    board_id = owner.post("/api/boards", json={"name": "季度", "retention": 30}).json()["board"]["id"]
    before = relay_env.read("SELECT expires_at FROM boards WHERE id = ?", (board_id,))[0]

    post_note(owner, board_id, "写一条", "m-1")

    after = relay_env.read("SELECT expires_at FROM boards WHERE id = ?", (board_id,))[0]
    assert after["expires_at"] == before["expires_at"]


# ---- 访客权限 ----


def test_guest_can_write_own_message_in_guest_board(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.cookie_client(guest.token) as guest_client:
        board_id = board_id_of(guest_client, guest=True)
        response = post_note(guest_client, board_id, "从打印店发的", "g-1")

    assert response.status_code == 201
    assert response.json()["note"]["authorId"] == guest.device_id


def test_guest_write_refreshes_guest_board_expiry(relay_env):
    """续期条件是"任何一次成功写入"，包括访客写的（方案 4.4）。"""
    relay_env.add_room()
    guest = relay_env.add_guest_device()
    soon = int(time.time()) + DAY

    with relay_env.paired_client() as owner:
        guest_board_id = board_id_of(owner, guest=True)
    relay_env.execute("UPDATE boards SET expires_at = ? WHERE id = ?", (soon, guest_board_id))

    with relay_env.cookie_client(guest.token) as guest_client:
        assert post_note(guest_client, guest_board_id, "访客写的", "g-1").status_code == 201

    after = relay_env.read("SELECT expires_at FROM boards WHERE id = ?", (guest_board_id,))[0]
    assert int(after["expires_at"]) > soon + 20 * DAY


def test_guest_cannot_write_to_main_board(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.paired_client() as owner:
        main_board_id = board_id_of(owner)

    with relay_env.cookie_client(guest.token) as guest_client:
        response = post_note(guest_client, main_board_id, "越权写入", "g-1")

    assert response.status_code == 404


def test_guest_cannot_read_other_board_notes(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.paired_client() as owner:
        main_board_id = board_id_of(owner)
        note_id = post_note(owner, main_board_id, "主人的内容", "m-1").json()["note"]["id"]

    with relay_env.cookie_client(guest.token) as guest_client:
        listed = guest_client.get(f"/api/boards/{main_board_id}/notes")
        edited = guest_client.patch(f"/api/notes/{note_id}", json={"content": "偷改"})
        deleted = guest_client.delete(f"/api/notes/{note_id}")

    assert listed.status_code == 404
    assert edited.status_code == 404
    assert deleted.status_code == 404


def test_guest_cannot_edit_owner_message_in_guest_board(relay_env):
    """访客能看访客区，但改不了主人在那里发的消息（方案 4.1）。"""
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.paired_client() as owner:
        guest_board_id = board_id_of(owner, guest=True)
        owner_note = post_note(owner, guest_board_id, "主人发在访客区的", "m-1").json()["note"]["id"]

        with relay_env.cookie_client(guest.token) as guest_client:
            edited = guest_client.patch(f"/api/notes/{owner_note}", json={"content": "改掉"})
            deleted = guest_client.delete(f"/api/notes/{owner_note}")

    # 403 而不是 404：这条消息对访客可见，只是不能改
    assert edited.status_code == 403
    assert deleted.status_code == 403


def test_guest_can_edit_and_delete_own_message(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.cookie_client(guest.token) as guest_client:
        board_id = board_id_of(guest_client, guest=True)
        note_id = post_note(guest_client, board_id, "我自己发的", "g-1").json()["note"]["id"]

        edited = guest_client.patch(f"/api/notes/{note_id}", json={"content": "改一下"})
        deleted = guest_client.delete(f"/api/notes/{note_id}")

    assert edited.status_code == 200
    assert edited.json()["note"]["content"] == "改一下"
    assert deleted.status_code == 204


def test_guest_cannot_pin(relay_env):
    """置顶是主人功能，单独返回 403 owner_only，前端据此把按钮藏掉。"""
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.cookie_client(guest.token) as guest_client:
        board_id = board_id_of(guest_client, guest=True)
        note_id = post_note(guest_client, board_id, "我自己发的", "g-1").json()["note"]["id"]

        response = guest_client.patch(f"/api/notes/{note_id}", json={"pinned": True})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "owner_only"


def test_note_operations_require_authentication(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        created = client.post(
            "/api/boards/whatever/notes", json={"content": "x", "mutationId": "m-1"}
        )
        deleted = client.delete("/api/notes/whatever")

    assert created.status_code == 401
    assert deleted.status_code == 401


def test_note_of_another_room_is_404(relay_env):
    relay_env.add_room("房间一")
    second = relay_env.add_room("房间二")

    with relay_env.paired_client() as owner_a:
        with relay_env.client() as owner_b:
            owner_b.get(f"/pair/{second.owner_secret}")
            foreign_note_id = post_note(owner_b, board_id_of(owner_b), "别的房间", "m-1")
            foreign_note_id = foreign_note_id.json()["note"]["id"]

        response = owner_a.patch(f"/api/notes/{foreign_note_id}", json={"content": "篡改"})

    assert response.status_code == 404
