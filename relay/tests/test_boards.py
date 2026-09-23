"""文本区的增删改查、生命周期与权限。

对应方案 16.1 / 16.2 / 16.4 中与区域相关的部分。三类断言最要紧：

1. **`rev` 单调递增且无空洞。** 它是客户端判断"要不要重拉快照"的唯一依据，
   一次少增或一次多增，都会让所有客户端要么漏更新、要么反复全量重拉；
2. **访客区绝不能被改名、排序或续期。** 它的 `expires_at` 由写入滑动维护，
   一旦被人工改成别的档位，DDL 的 CHECK 和 4.4 的规则会同时被破坏；
3. **跨房间与不可见资源一律 404**，不因为"存在但无权"而返回 403。
"""

from __future__ import annotations

import time

import pytest

from app.boards import BOARD_LIMIT_PER_ROOM, BOARD_NAME_MAX, SECONDS_PER_DAY

DAY = SECONDS_PER_DAY


def fetch_boards(client, **params) -> list[dict]:
    response = client.get("/api/boards", params=params)
    assert response.status_code == 200, response.text
    return response.json()["boards"]


def board_id_of(client, *, guest: bool) -> str:
    return next(board["id"] for board in fetch_boards(client) if board["isGuest"] is guest)


@pytest.fixture
def owner(relay_env):
    relay_env.add_room()
    with relay_env.paired_client() as client:
        yield client


# ---- 房间自带的两个区域 ----


def test_new_room_has_daily_and_guest_board(owner):
    boards = fetch_boards(owner)

    assert [board["name"] for board in boards] == ["日常", "访客区"]
    daily, guest = boards
    # 日常区取永久：它是默认落地页，静默归档等于让人突然失去写入能力
    assert daily["retention"] == 0
    assert daily["expiresAt"] is None
    assert daily["isGuest"] is False
    assert guest["isGuest"] is True
    assert guest["retention"] == 30
    assert guest["expiresAt"] is not None
    assert guest["status"] == "active"
    assert [board["sortOrder"] for board in boards] == [0, 1]


def test_boards_are_ordered_by_sort_order(owner):
    created = owner.post("/api/boards", json={"name": "工作", "retention": 30})
    assert created.status_code == 201

    names = [board["name"] for board in fetch_boards(owner)]
    # 新建的排在最后（sort_order = max + 1），不与既有顺序打架
    assert names == ["日常", "访客区", "工作"]


# ---- 新建 ----


def test_create_board_returns_201_and_bumps_rev(owner, relay_env):
    before = relay_env.room_rev()
    response = owner.post("/api/boards", json={"name": "临时", "retention": 30})

    assert response.status_code == 201
    board = response.json()["board"]
    assert board["retention"] == 30
    assert board["status"] == "active"
    assert board["isGuest"] is False
    assert board["noteCount"] == 0
    assert board["expired"] is False
    # expiresAt 由服务端时间算出，不允许客户端传
    assert abs(board["expiresAt"] - (int(time.time()) + 30 * DAY)) <= 5
    assert relay_env.room_rev() == before + 1


def test_create_board_defaults_to_60_days(owner):
    response = owner.post("/api/boards", json={"name": "季度"})

    assert response.status_code == 201
    assert response.json()["board"]["retention"] == 60


def test_create_board_with_permanent_retention_has_no_expiry(owner):
    response = owner.post("/api/boards", json={"name": "长久", "retention": 0})

    assert response.status_code == 201
    assert response.json()["board"]["expiresAt"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * (BOARD_NAME_MAX + 1)},
        {"name": "换\n行"},
        {"name": 123},
        {},
        {"name": "改名", "retention": 15},
        {"name": "改名", "retention": -1},
        {"name": "改名", "retention": "30"},
        # 拼错的字段名必须报错而不是被忽略——否则"我设了 30 天"和
        # "实际拿到 60 天"之间没有任何提示
        {"name": "改名", "ratention": 30},
    ],
    ids=[
        "empty-name",
        "blank-name",
        "long-name",
        "newline",
        "not-a-string",
        "missing-name",
        "bad-retention",
        "negative-retention",
        "string-retention",
        "typo-field",
    ],
)
def test_create_board_rejects_invalid_payload(owner, payload):
    response = owner.post("/api/boards", json=payload)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"].startswith(("invalid_", "bad_request"))


def test_create_board_respects_limit(owner, relay_env):
    # 已有 2 个（日常、访客区），再补到上限
    for index in range(BOARD_LIMIT_PER_ROOM - 2):
        response = owner.post("/api/boards", json={"name": f"区域{index}"})
        assert response.status_code == 201, response.text

    overflow = owner.post("/api/boards", json={"name": "溢出"})
    assert overflow.status_code == 409
    assert overflow.json()["error"]["code"] == "board_limit_reached"


# ---- 改名 / 排序 / 续期 ----


def test_patch_renames_board(owner, relay_env):
    board_id = board_id_of(owner, guest=False)
    before = relay_env.room_rev()

    response = owner.patch(f"/api/boards/{board_id}", json={"name": "  我的日常  "})

    assert response.status_code == 200
    assert response.json()["board"]["name"] == "我的日常"
    assert relay_env.room_rev() == before + 1


def test_patch_retention_recomputes_expiry(owner, relay_env):
    board_id = owner.post("/api/boards", json={"name": "季度", "retention": 60}).json()["board"]["id"]

    response = owner.patch(f"/api/boards/{board_id}", json={"retention": 30})

    assert response.status_code == 200
    board = response.json()["board"]
    assert board["retention"] == 30
    assert abs(board["expiresAt"] - (int(time.time()) + 30 * DAY)) <= 5


def test_patch_same_retention_is_a_renewal(owner):
    """续期就是把 `expires_at` 推成「当前时间 + 原保留天数」（方案 4.2）。

    这里不重述一个已到期区域被救回来的场景——那样要伪造时间，而 SLA 上
    真正需要保证的只是"同样的入参产生同样的推后效果"。
    """
    board_id = owner.post("/api/boards", json={"name": "季度", "retention": 30}).json()["board"]["id"]
    first = owner.patch(f"/api/boards/{board_id}", json={"retention": 30}).json()["board"]
    second = owner.patch(f"/api/boards/{board_id}", json={"retention": 30}).json()["board"]

    assert second["expiresAt"] >= first["expiresAt"]
    assert second["retention"] == 30


def test_patch_to_permanent_clears_expiry(owner):
    board_id = owner.post("/api/boards", json={"name": "季度", "retention": 30}).json()["board"]["id"]

    board = owner.patch(f"/api/boards/{board_id}", json={"retention": 0}).json()["board"]

    assert board["retention"] == 0
    assert board["expiresAt"] is None


def test_patch_sort_order_changes_listing_order(owner):
    board_id = owner.post("/api/boards", json={"name": "工作"}).json()["board"]["id"]

    owner.patch(f"/api/boards/{board_id}", json={"sortOrder": -1})

    assert [board["name"] for board in fetch_boards(owner)][0] == "工作"


def test_patch_with_empty_body_is_rejected(owner):
    board_id = board_id_of(owner, guest=False)
    response = owner.patch(f"/api/boards/{board_id}", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] in ("empty_update", "bad_request")


def test_expired_board_can_be_renewed(owner, relay_env):
    """到期但尚未归档的区域，续期是唯一能救回它的手段，必须放行。

    刻意不用日常区来演这个场景：它是永久的（`retention = 0`），而 DDL 的
    CHECK 约束不允许"永久"与 `expires_at` 同时存在。续期这条路径本来就只
    对 30/60 天的区域有意义。
    """
    board_id = owner.post("/api/boards", json={"name": "季度", "retention": 30}).json()["board"]["id"]
    relay_env.execute("UPDATE boards SET expires_at = ? WHERE id = ?", (1, board_id))

    boards = fetch_boards(owner)
    target = next(board for board in boards if board["id"] == board_id)
    assert target["expired"] is True

    response = owner.patch(f"/api/boards/{board_id}", json={"retention": 30})

    assert response.status_code == 200
    assert response.json()["board"]["expired"] is False


def test_archived_board_rejects_rename_and_sort(owner, relay_env):
    board_id = owner.post("/api/boards", json={"name": "季度", "retention": 30}).json()["board"]["id"]
    relay_env.execute(
        "UPDATE boards SET status = 'archived', archived_at = ? WHERE id = ?",
        (int(time.time()), board_id),
    )

    renamed = owner.patch(f"/api/boards/{board_id}", json={"name": "新名字"})
    sorted_ = owner.patch(f"/api/boards/{board_id}", json={"sortOrder": 9})

    assert renamed.status_code == 409
    assert renamed.json()["error"]["code"] == "board_archived"
    assert sorted_.status_code == 409


def test_patch_unknown_board_is_404(owner):
    response = owner.patch("/api/boards/not-a-board", json={"name": "x"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---- 访客区不可变更（方案 4.4） ----


@pytest.mark.parametrize(
    "payload",
    [{"name": "改名"}, {"retention": 60}, {"sortOrder": 5}],
    ids=["rename", "retention", "sort"],
)
def test_guest_board_rejects_every_mutation(owner, payload):
    guest_id = board_id_of(owner, guest=True)
    before = owner.get("/api/boards").json()

    response = owner.patch(f"/api/boards/{guest_id}", json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "guest_board_immutable"
    # 状态一个字都没变
    assert owner.get("/api/boards").json() == before


def test_guest_board_status_is_always_active(owner):
    guest = next(board for board in fetch_boards(owner) if board["isGuest"])
    assert guest["status"] == "active"
    assert guest["archivedAt"] is None


# ---- 权限 ----


def test_guest_sees_only_guest_board(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()
    owner_board_id = None

    with relay_env.paired_client() as owner:
        owner_board_id = board_id_of(owner, guest=False)

    with relay_env.cookie_client(guest.token) as guest_client:
        visible = fetch_boards(guest_client)

    assert [board["isGuest"] for board in visible] == [True]
    assert all(board["id"] != owner_board_id for board in visible)


def test_guest_cannot_create_board(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.cookie_client(guest.token) as guest_client:
        response = guest_client.post("/api/boards", json={"name": "偷偷建的"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_guest_cannot_patch_guest_board(relay_env):
    """访客既不该能改区域，也不该拿到"访客区不可变更"这种内部语义。"""
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.paired_client() as owner:
        guest_board_id = board_id_of(owner, guest=True)

    with relay_env.cookie_client(guest.token) as guest_client:
        response = guest_client.patch(f"/api/boards/{guest_board_id}", json={"name": "改名"})

    assert response.status_code == 403


def test_guest_cannot_address_other_boards(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.paired_client() as owner:
        owner_board_id = board_id_of(owner, guest=False)

    with relay_env.cookie_client(guest.token) as guest_client:
        renamed = guest_client.patch(f"/api/boards/{owner_board_id}", json={"name": "越权"})
        notes = guest_client.get(f"/api/boards/{owner_board_id}/notes")

    # 这两条返回码不同，区别是刻意的：
    # - 改名是**动作级**权限，访客不能修改任何区域，所以在读库之前就被拦下，
    #   返回 403——它与"这个 id 是否存在"无关，因此不泄漏存在性；
    # - 读消息是**资源级**可见性，主人区域对访客不存在，返回 404。
    assert renamed.status_code == 403
    assert notes.status_code == 404


def test_board_of_another_room_is_404(relay_env):
    first = relay_env.add_room("房间一")
    second = relay_env.add_room("房间二")

    with relay_env.paired_client() as owner_a:
        with relay_env.client() as owner_b:
            owner_b.get(f"/pair/{second.owner_secret}")
            foreign_board_id = board_id_of(owner_b, guest=False)
        response = owner_a.patch(f"/api/boards/{foreign_board_id}", json={"name": "篡改"})

    assert first.room_id != second.room_id
    assert response.status_code == 404


def test_boards_require_authentication(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        listed = client.get("/api/boards")
        created = client.post("/api/boards", json={"name": "x"})

    assert listed.status_code == 401
    assert created.status_code == 401


# ---- rev 与分页 ----


def test_reads_do_not_bump_rev(owner, relay_env):
    before = relay_env.room_rev()

    fetch_boards(owner)
    board_id = board_id_of(owner, guest=False)
    owner.get(f"/api/boards/{board_id}/notes")
    owner.get("/api/devices")

    assert relay_env.room_rev() == before


def test_rev_is_dense_across_many_writes(owner, relay_env):
    """连续 10 次写，rev 必须恰好 +10，不多不少。

    "多"和"少"都会坏事：多了客户端会以为丢了推送而白拉快照，少了它会
    以为状态没变而漏掉更新。空洞同理。
    """
    before = relay_env.room_rev()
    board_id = board_id_of(owner, guest=False)

    for index in range(5):
        created = owner.post(
            f"/api/boards/{board_id}/notes",
            json={"content": f"第 {index} 条", "mutationId": f"m-{index}"},
        )
        assert created.status_code == 201, created.text

    owner.patch(f"/api/boards/{board_id}", json={"name": "改个名"})

    for index in range(4):
        note_id = owner.get(f"/api/boards/{board_id}/notes").json()["notes"][0]["id"]
        owner.delete(f"/api/notes/{note_id}")

    assert relay_env.room_rev() == before + 10


def test_board_pagination_walks_without_gaps_or_repeats(owner):
    names = [f"区域{index}" for index in range(5)]
    for name in names:
        assert owner.post("/api/boards", json={"name": name}).status_code == 201

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = owner.get("/api/boards", params=params).json()
        seen.extend(board["id"] for board in body["boards"])
        cursor = body["nextCursor"]
        if cursor is None:
            break

    # 日常 + 访客区 + 5 个新建 = 7
    assert len(seen) == 7
    assert len(set(seen)) == 7


def test_invalid_cursor_is_400(owner):
    response = owner.get("/api/boards", params={"cursor": "not-a-board"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_limit_above_max_is_rejected(owner):
    """超上限时明确报错，不静默截断——静默截断会让分页算错而无人察觉。"""
    response = owner.get("/api/boards", params={"limit": 100000})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_limit"


def test_boards_endpoint_reports_current_rev(owner, relay_env):
    body = owner.get("/api/boards").json()

    assert body["rev"] == relay_env.room_rev()
