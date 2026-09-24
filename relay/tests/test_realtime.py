"""WebSocket 推送与广播（方案 4.3、3.3、第 10 节）。

测试分两层：

- **端到端**（TestClient 的 `websocket_connect`）验证协议本身：来源与会话校验、
  `hello.ok`、心跳、变更推送、撤销后断开；
- **纯逻辑**（直接构造 `ConnectionManager` 与假 WebSocket）验证连接表的分组与
  限额——"换个房间都不会更慢"这类断言用 HTTP 造不出来，因为每个 TestClient
  实例自带一个 app，连接表不共享。

一处必须先说清的测试限制：**所有 TestClient 连接的来源 IP 都是 `testclient`**，
所以"单房间 20 / 单 IP 10"这两个限额得用配置项调小才测得动。这也正是那两个
配置项存在的原因（生产保持方案默认值）。
"""

from __future__ import annotations

import time
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app.events import EVENT_NOTE_CREATED, RoomEvent
from app.realtime import Connection, ConnectionManager
from app.state import RuntimeState
from conftest import RelayEnv


# ---------------------------------------------------------------- 工具


class FakeWebSocket:
    """只记录"发了什么、关了几次"的假连接。"""

    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[dict] = []
        self.closed: list[int] = []
        self.fail_send = fail_send

    async def send_json(self, message: dict) -> None:
        if self.fail_send:
            raise RuntimeError("对端已断开")
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def _make_connection(
    *,
    room_id: str,
    device_id: str,
    ip: str,
    fail_send: bool = False,
) -> Connection:
    socket = FakeWebSocket(fail_send=fail_send)
    return Connection(
        id=uuid.uuid4().hex,
        room_id=room_id,
        device_id=device_id,
        session_id=uuid.uuid4().hex,
        role="owner",
        ip=ip,
        websocket=socket,  # type: ignore[arg-type]
        connected_at=int(time.time()),
        last_auth_at=int(time.time()),
    )


def _make_board(client, name: str = "工作") -> dict:
    response = client.post("/api/boards", json={"name": name, "retention": 0})
    assert response.status_code == 201, response.text
    return response.json()["board"]


def _make_note(client, board_id: str, content: str, mutation_id: str | None = None) -> dict:
    response = client.post(
        f"/api/boards/{board_id}/notes",
        json={"content": content, "mutationId": mutation_id or uuid.uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()["note"]


def _hello(ws) -> dict:
    message = ws.receive_json()
    assert message["t"] == "hello.ok", message
    return message


# ---------------------------------------------------------------- 握手


def test_ws_requires_session(relay_env: RelayEnv) -> None:
    """未配对设备连不上——Cookie 是唯一的凭证，URL 里不接受任何令牌。"""
    relay_env.add_room()
    with relay_env.client() as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws"):
                pass


def test_ws_rejects_foreign_origin(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "null"}):
                pass


def test_ws_accepts_same_origin(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert _hello(ws)["role"] == "owner"


def test_ws_greets_with_current_rev(relay_env: RelayEnv) -> None:
    """`hello.ok` 里的 rev 就是服务端此刻的 rev，客户端据此决定要不要拉快照。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        _make_board(client, "先建一个")
        with client.websocket_connect("/ws") as ws:
            greeting = _hello(ws)

    assert greeting["rev"] == relay_env.room_rev() == 1
    assert greeting["serverTime"] >= 0


def test_ws_answers_hello_with_another_greeting(relay_env: RelayEnv) -> None:
    """按方案 10 的时序发 `hello` 也能拿到 `hello.ok`。

    服务端已经主动发过一次，所以同一条连接上会出现两次 `hello.ok`。这是刻意
    保留的兼容：客户端只需把它当幂等的"记下服务端 rev"，两种实现顺序就都能跑。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        with client.websocket_connect("/ws") as ws:
            first = _hello(ws)
            ws.send_json({"t": "hello", "rev": 0})
            second = _hello(ws)

    assert first["rev"] == second["rev"] == 0


def test_ws_heartbeat(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            ws.send_json({"t": "ping"})
            assert ws.receive_json() == {"t": "pong"}


def test_ws_reports_bad_messages_without_closing(relay_env: RelayEnv) -> None:
    """坏消息回 error，但连接留着——为一条打错的帧断线只会制造无谓的重连。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)

            ws.send_text("这不是 JSON")
            assert ws.receive_json()["code"] == "bad_message"

            ws.send_json({"t": "什么"})
            assert ws.receive_json()["code"] == "bad_message"

            ws.send_json([1, 2, 3])
            assert ws.receive_json()["code"] == "bad_message"

            ws.send_json({"t": "ping"})
            assert ws.receive_json() == {"t": "pong"}


# ---------------------------------------------------------------- 推送


def test_ws_receives_note_lifecycle(relay_env: RelayEnv) -> None:
    """新增、编辑、删除各推一条，`rev` 每次都精确 +1（方案 4.3）。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _make_board(client)
        with client.websocket_connect("/ws") as ws:
            base_rev = _hello(ws)["rev"]

            note = _make_note(client, board["id"], "你好")
            created = ws.receive_json()
            assert created["t"] == "changed"
            assert created["event"] == EVENT_NOTE_CREATED
            assert created["rev"] == base_rev + 1
            assert created["payload"]["note"]["content"] == "你好"
            assert created["payload"]["note"]["id"] == note["id"]

            response = client.patch(f"/api/notes/{note['id']}", json={"content": "改过了"})
            assert response.status_code == 200
            updated = ws.receive_json()
            assert updated["event"] == "note.updated"
            assert updated["rev"] == created["rev"] + 1
            assert updated["payload"]["note"]["content"] == "改过了"

            assert client.delete(f"/api/notes/{note['id']}").status_code == 204
            deleted = ws.receive_json()
            assert deleted["event"] == "note.deleted"
            assert deleted["rev"] == updated["rev"] + 1
            # 删除事件不带正文：内容已不该在任何客户端显示，再分发一遍只是徒增暴露面。
            assert deleted["payload"] == {"noteId": note["id"], "boardId": board["id"]}


def test_ws_receives_board_events(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        with client.websocket_connect("/ws") as ws:
            base_rev = _hello(ws)["rev"]

            board = _make_board(client, "新区域")
            created = ws.receive_json()
            assert created["event"] == "board.created"
            assert created["rev"] == base_rev + 1
            assert created["payload"]["board"]["name"] == "新区域"

            response = client.patch(f"/api/boards/{board['id']}", json={"name": "改名了"})
            assert response.status_code == 200
            renamed = ws.receive_json()
            assert renamed["event"] == "board.updated"
            assert renamed["rev"] == created["rev"] + 1
            assert renamed["payload"]["board"]["name"] == "改名了"


def test_ws_rev_has_no_gaps(relay_env: RelayEnv) -> None:
    """连续多次写入，推送里的 rev 必须是连续整数。

    这是客户端"rev 正好 +1 就增量应用"的依据。多一次递增会让客户端白拉一次
    快照，少一次会让它漏掉一条更新。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _make_board(client)
        with client.websocket_connect("/ws") as ws:
            base = _hello(ws)["rev"]
            for index in range(5):
                _make_note(client, board["id"], f"第{index}条")

            revs = [ws.receive_json()["rev"] for _ in range(5)]

    assert revs == [base + offset for offset in range(1, 6)]


def test_ws_idempotent_replay_does_not_broadcast(relay_env: RelayEnv) -> None:
    """幂等重放不产生推送（也没有递增 rev）。

    判定手法：重放之后立刻发一个 ping，然后看收到的**第一条**消息是什么。
    消息按到达顺序排队，所以如果有广播，它会插在 pong 前面。不需要靠
    "等一段时间看有没有东西来"这种会拖慢测试、还未必可靠的写法。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _make_board(client)
        with client.websocket_connect("/ws") as ws:
            _hello(ws)

            mutation_id = uuid.uuid4().hex
            _make_note(client, board["id"], "只发一次", mutation_id)
            assert ws.receive_json()["event"] == EVENT_NOTE_CREATED

            replay = client.post(
                f"/api/boards/{board['id']}/notes",
                json={"content": "只发一次", "mutationId": mutation_id},
            )
            assert replay.status_code == 200

            ws.send_json({"t": "ping"})
            assert ws.receive_json() == {"t": "pong"}


def test_ws_fanout_to_all_devices_in_room(relay_env: RelayEnv) -> None:
    """同房间的两条连接都收到同一次变更。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _make_board(client)
        with client.websocket_connect("/ws") as first, client.websocket_connect("/ws") as second:
            _hello(first)
            _hello(second)

            _make_note(client, board["id"], "给你们俩")

            for ws in (first, second):
                message = ws.receive_json()
                assert message["event"] == EVENT_NOTE_CREATED
                assert message["payload"]["note"]["content"] == "给你们俩"


# ---------------------------------------------------------------- 限额与超时


def test_ws_room_connection_limit(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client(RELAY_WS_ROOM_LIMIT="1") as client:
        with client.websocket_connect("/ws") as first:
            _hello(first)
            with client.websocket_connect("/ws") as second:
                # 被拒时会先收到一条说明原因的消息，然后才断开——客户端据此
                # 能把"连太多了"和"连不上"区分开。
                refusal = second.receive_json()
                assert refusal["code"] == "room_connection_limit"
                assert second.receive()["type"] == "websocket.close"


def test_ws_ip_connection_limit(relay_env: RelayEnv) -> None:
    """单 IP 上限独立于单房间上限。

    TestClient 里所有连接的来源都是 `testclient`，所以把房间上限放宽、只收紧
    IP 上限，测的就是 IP 这一条分支。
    """
    relay_env.add_room()
    with relay_env.paired_client(
        RELAY_WS_ROOM_LIMIT="10", RELAY_WS_IP_LIMIT="1"
    ) as client:
        with client.websocket_connect("/ws") as first:
            _hello(first)
            with client.websocket_connect("/ws") as second:
                assert second.receive_json()["code"] == "ip_connection_limit"


def test_ws_idle_timeout_closes_connection(relay_env: RelayEnv) -> None:
    """方案 10：60 秒无活动即关闭。测试里把阈值压到 1 秒。"""
    relay_env.add_room()
    with relay_env.paired_client(RELAY_WS_IDLE_TIMEOUT_SEC="1") as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            time.sleep(1.6)
            closing = ws.receive()

    assert closing["type"] == "websocket.close"
    assert closing["code"] == 1001


def test_ws_activity_postpones_idle_timeout(relay_env: RelayEnv) -> None:
    """有活动就不该被超时关掉。"""
    relay_env.add_room()
    with relay_env.paired_client(RELAY_WS_IDLE_TIMEOUT_SEC="2") as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            for _ in range(3):
                time.sleep(0.8)
                ws.send_json({"t": "ping"})
                assert ws.receive_json() == {"t": "pong"}


def test_healthz_reports_live_connection_count(relay_env: RelayEnv) -> None:
    """M1 期间 `websocketConnections` 恒为 0，现在它反映真实连接数。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        assert client.get("/healthz").json()["websocketConnections"] == 0
        with client.websocket_connect("/ws") as first:
            _hello(first)
            assert client.get("/healthz").json()["websocketConnections"] == 1
            with client.websocket_connect("/ws") as second:
                _hello(second)
                assert client.get("/healthz").json()["websocketConnections"] == 2
            assert client.get("/healthz").json()["websocketConnections"] == 1
        assert client.get("/healthz").json()["websocketConnections"] == 0


# ---------------------------------------------------------------- 撤销设备


def test_revoking_device_closes_its_websocket(relay_env: RelayEnv) -> None:
    """撤销后不是等心跳，而是当场断开（方案 3.3 的下限是 60 秒，这里更严）。

    撤销的用意就是立刻切断。若等到下一次心跳，那台设备在这段时间里仍在接收
    房间的全部推送内容。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            current = client.get("/api/devices").json()["currentDeviceId"]

            assert client.delete(f"/api/devices/{current}").status_code == 204

            notice = ws.receive_json()
            assert notice["code"] == "session_revoked"
            closing = ws.receive()

    assert closing["type"] == "websocket.close"
    assert closing["code"] == 4401


def test_revoke_other_devices_keeps_current_connection(relay_env: RelayEnv) -> None:
    """`DELETE /api/devices` 撤销其他设备，当前设备的连接不受影响。"""
    relay_env.add_room()
    guest = relay_env.add_guest_device()

    with relay_env.paired_client() as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            response = client.delete("/api/devices")
            assert response.status_code == 200
            assert response.json()["revoked"] == 1

            # 当前连接还活着：ping 得到 pong。
            ws.send_json({"t": "ping"})
            assert ws.receive_json() == {"t": "pong"}

            # 被撤销的访客设备连不上任何接口了。
            with relay_env.cookie_client(guest.token) as guest_client:
                assert guest_client.get("/api/snapshot").status_code == 401


# ---------------------------------------------------------------- 连接表逻辑


async def test_manager_never_broadcasts_across_rooms() -> None:
    """跨房间泄漏消息是这个模块唯一不能犯的错。"""
    state = RuntimeState()
    manager = ConnectionManager(state=state, room_limit=20, ip_limit=10)
    mine = _make_connection(room_id="room-a", device_id="dev-1", ip="10.0.0.1")
    theirs = _make_connection(room_id="room-b", device_id="dev-2", ip="10.0.0.2")
    manager.register(mine)
    manager.register(theirs)

    await manager.publish(
        [
            RoomEvent(
                room_id="room-a",
                rev=7,
                event=EVENT_NOTE_CREATED,
                payload={"note": {"id": "n1"}},
            )
        ]
    )

    assert mine.websocket.sent == [
        {
            "t": "changed",
            "rev": 7,
            "event": EVENT_NOTE_CREATED,
            "payload": {"note": {"id": "n1"}},
        }
    ]
    assert theirs.websocket.sent == []


async def test_manager_publish_to_empty_room_is_silent() -> None:
    """没人在听不该报错，也不该留下任何东西。"""
    manager = ConnectionManager(state=RuntimeState(), room_limit=20, ip_limit=10)
    await manager.publish(
        [RoomEvent(room_id="nobody", rev=1, event=EVENT_NOTE_CREATED, payload={})]
    )
    assert manager.count == 0


async def test_manager_unregisters_broken_connections() -> None:
    """发不出去的连接当场摘掉，不占配额。"""
    state = RuntimeState()
    manager = ConnectionManager(state=state, room_limit=20, ip_limit=10)
    broken = _make_connection(
        room_id="room-a", device_id="dev-1", ip="10.0.0.1", fail_send=True
    )
    manager.register(broken)
    assert state.websocket_connections == 1

    await manager.publish(
        [RoomEvent(room_id="room-a", rev=1, event=EVENT_NOTE_CREATED, payload={})]
    )

    assert manager.count == 0
    assert state.websocket_connections == 0


async def test_manager_unregister_is_idempotent() -> None:
    """收发循环的 finally 与广播失败两条路径都会摘除，重复调用必须无害。"""
    state = RuntimeState()
    manager = ConnectionManager(state=state, room_limit=20, ip_limit=10)
    conn = _make_connection(room_id="room-a", device_id="dev-1", ip="10.0.0.1")
    manager.register(conn)

    manager.unregister(conn.id)
    manager.unregister(conn.id)
    manager.unregister("根本不存在的 id")

    assert manager.count == 0
    assert state.websocket_connections == 0
    # 摘干净之后再连一条同房间、同 IP 的，配额应该完整可用。
    assert manager.register(_make_connection(room_id="room-a", device_id="dev-2", ip="10.0.0.1")) is None


async def test_manager_room_limit_counts_only_that_room() -> None:
    manager = ConnectionManager(state=RuntimeState(), room_limit=1, ip_limit=10)
    assert manager.register(_make_connection(room_id="a", device_id="d1", ip="1.1.1.1")) is None
    assert (
        manager.register(_make_connection(room_id="a", device_id="d2", ip="1.1.1.2"))
        == "room_connection_limit"
    )
    # 另一个房间不受影响。
    assert manager.register(_make_connection(room_id="b", device_id="d3", ip="1.1.1.3")) is None


async def test_manager_ip_limit_counts_across_rooms() -> None:
    """同一个 IP 上的连接即使分属不同房间，也要一起计数（方案 10）。"""
    manager = ConnectionManager(state=RuntimeState(), room_limit=10, ip_limit=1)
    assert manager.register(_make_connection(room_id="a", device_id="d1", ip="1.1.1.1")) is None
    assert (
        manager.register(_make_connection(room_id="b", device_id="d2", ip="1.1.1.1"))
        == "ip_connection_limit"
    )
    assert manager.register(_make_connection(room_id="b", device_id="d3", ip="2.2.2.2")) is None


async def test_manager_close_connections_selects_by_device() -> None:
    manager = ConnectionManager(state=RuntimeState(), room_limit=20, ip_limit=10)
    target = _make_connection(room_id="a", device_id="doomed", ip="1.1.1.1")
    other = _make_connection(room_id="a", device_id="safe", ip="1.1.1.2")
    manager.register(target)
    manager.register(other)

    closed = await manager.close_connections(device_ids=["doomed"])

    assert closed == 1
    assert target.websocket.sent[0]["code"] == "session_revoked"
    assert target.websocket.closed == [4401]
    assert other.websocket.sent == []
    assert manager.count == 1


async def test_manager_close_connections_respects_room_boundary() -> None:
    """按房间关闭时不得越界——否则"撤销其他设备"会误伤别的房间。"""
    manager = ConnectionManager(state=RuntimeState(), room_limit=20, ip_limit=10)
    same_room = _make_connection(room_id="a", device_id="d1", ip="1.1.1.1")
    other_room = _make_connection(room_id="b", device_id="d1", ip="1.1.1.2")
    manager.register(same_room)
    manager.register(other_room)

    closed = await manager.close_connections(room_id="a", keep_device_id="nobody")

    assert closed == 1
    assert other_room.websocket.closed == []


async def test_manager_close_except_keeps_named_device() -> None:
    manager = ConnectionManager(state=RuntimeState(), room_limit=20, ip_limit=10)
    keeper = _make_connection(room_id="a", device_id="keep", ip="1.1.1.1")
    goner = _make_connection(room_id="a", device_id="drop", ip="1.1.1.2")
    manager.register(keeper)
    manager.register(goner)

    closed = await manager.close_connections(room_id="a", keep_device_id="keep")

    assert closed == 1
    assert keeper.websocket.closed == []
    assert goner.websocket.closed == [4401]


def test_client_ip_prefers_forwarded_header() -> None:
    from app.realtime import client_ip

    assert client_ip(forwarded_for="203.0.113.7, 10.0.0.1", peer_host="127.0.0.1") == "203.0.113.7"
    assert client_ip(forwarded_for=None, peer_host="127.0.0.1") == "127.0.0.1"
    assert client_ip(forwarded_for="   ", peer_host=None) == "unknown"
    assert client_ip(forwarded_for=",", peer_host="127.0.0.1") == "127.0.0.1"
