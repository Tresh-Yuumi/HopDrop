"""设备管理与权限边界。

方案 3.3 的五项操作、16.1 的权限用例都落在这里。重点验证两件事：

1. **撤销是立即生效的**——不是"下次过期才生效"，所以 HTTP 请求当场 401；
2. **跨房间的资源一律 404**，不因为"存在但无权访问"而返回 403（那会泄漏存在性）。
"""

from __future__ import annotations

import pytest


def pair_and_device_id(env, client, secret: str | None = None) -> str:
    client.get(f"/pair/{secret or env.room.owner_secret}")
    response = client.get("/api/devices")
    assert response.status_code == 200, response.text
    return response.json()["currentDeviceId"]


# ---- 列表 ----


def test_device_list_marks_current_device_and_omits_secrets(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner, relay_env.client() as second:
        owner_id = pair_and_device_id(relay_env, owner)
        second_id = pair_and_device_id(relay_env, second)

        body = owner.get("/api/devices").json()

    assert body["currentDeviceId"] == owner_id
    ids = {device["id"] for device in body["devices"]}
    assert ids == {owner_id, second_id}

    current = [device for device in body["devices"] if device["isCurrent"]]
    assert len(current) == 1
    assert current[0]["id"] == owner_id
    assert current[0]["role"] == "owner"

    # 列表里不能出现任何凭证形态的字段
    for device in body["devices"]:
        assert "token" not in device
        assert "ua" not in device


def test_devices_are_sorted_by_pairing_time(relay_env):
    relay_env.add_room()
    with relay_env.client() as first, relay_env.client() as second:
        first_id = pair_and_device_id(relay_env, first)
        second_id = pair_and_device_id(relay_env, second)
        devices = first.get("/api/devices").json()["devices"]

    assert [device["id"] for device in devices] == [first_id, second_id]


# ---- 改名 ----


def test_owner_can_rename_any_device(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner, relay_env.client() as second:
        owner_id = pair_and_device_id(relay_env, owner)
        second_id = pair_and_device_id(relay_env, second)

        renamed = owner.patch(f"/api/devices/{second_id}", json={"name": "客厅的平板"})
        devices = owner.get("/api/devices").json()["devices"]

    assert renamed.status_code == 200
    names = {device["id"]: device["name"] for device in devices}
    assert names[second_id] == "客厅的平板"
    assert names[owner_id] != "客厅的平板"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 65},
        {"name": "换\n行"},
        {"name": "制表\t符"},
        {"name": 123},
        {},
    ],
    ids=["empty", "spaces", "too-long", "newline", "tab", "not-a-string", "missing"],
)
def test_rename_rejects_structural_names(relay_env, payload):
    relay_env.add_room()
    with relay_env.client() as owner:
        device_id = pair_and_device_id(relay_env, owner)
        response = owner.patch(f"/api/devices/{device_id}", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] in ("invalid_device_name", "bad_request")


def test_rename_accepts_unicode_and_trims_whitespace(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner:
        device_id = pair_and_device_id(relay_env, owner)
        response = owner.patch(f"/api/devices/{device_id}", json={"name": "  阿渡的电脑 🚀  "})

    assert response.status_code == 200
    assert relay_env.read("SELECT name FROM devices")[0]["name"] == "阿渡的电脑 🚀"


def test_rename_unknown_device_returns_404(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner:
        pair_and_device_id(relay_env, owner)
        response = owner.patch("/api/devices/不存在", json={"name": "x"})

    assert response.status_code == 404


def test_rename_device_of_another_room_returns_404(relay_env):
    first = relay_env.add_room("房间一")
    second = relay_env.add_room("房间二")

    with relay_env.client() as owner_a, relay_env.client() as owner_b:
        pair_and_device_id(relay_env, owner_a)
        foreign_id = pair_and_device_id(relay_env, owner_b, secret=second.owner_secret)

        renamed = owner_a.patch(f"/api/devices/{foreign_id}", json={"name": "篡改"})
        deleted = owner_a.delete(f"/api/devices/{foreign_id}")

    assert first.room_id != second.room_id
    assert renamed.status_code == 404
    assert deleted.status_code == 404


# ---- 权限边界 ----


def test_guest_cannot_list_or_delete_devices(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()
    with relay_env.cookie_client(guest.token) as guest_client:
        listed = guest_client.get("/api/devices")
        deleted = guest_client.delete(f"/api/devices/{guest.device_id}")
        nuked = guest_client.delete("/api/devices")

    # 403 而不是 404：设备列表对访客是"存在但无权"，不是不存在
    assert listed.status_code == 403
    assert deleted.status_code == 403
    assert nuked.status_code == 403


def test_guest_can_rename_itself_but_not_others(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()
    with relay_env.client() as owner:
        owner_id = pair_and_device_id(relay_env, owner)
        with relay_env.cookie_client(guest.token) as guest_client:
            itself = guest_client.patch(
                f"/api/devices/{guest.device_id}", json={"name": "打印店的笔记本"}
            )
            other = guest_client.patch(f"/api/devices/{owner_id}", json={"name": "越权"})

    assert itself.status_code == 200
    assert other.status_code == 403


# ---- 撤销 ----


def test_revoke_device_invalidates_its_session_immediately(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner, relay_env.client() as victim:
        pair_and_device_id(relay_env, owner)
        victim_id = pair_and_device_id(relay_env, victim)
        assert victim.get("/api/devices").status_code == 200

        revoked = owner.delete(f"/api/devices/{victim_id}")
        after_revoke = victim.get("/api/devices")
        owner_still_ok = owner.get("/api/devices")

    assert revoked.status_code == 204
    assert after_revoke.status_code == 401
    # 撤销一台不能把主人自己也踢掉
    assert owner_still_ok.status_code == 200
    assert len(relay_env.read("SELECT id FROM devices WHERE revoked_at IS NULL")) == 1


def test_revoked_device_disappears_from_list(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner, relay_env.client() as second:
        pair_and_device_id(relay_env, owner)
        second_id = pair_and_device_id(relay_env, second)
        owner.delete(f"/api/devices/{second_id}")
        devices = owner.get("/api/devices").json()["devices"]

    assert [device["id"] for device in devices] != []
    assert all(device["id"] != second_id for device in devices)


def test_revoke_unknown_device_returns_404(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner:
        pair_and_device_id(relay_env, owner)
        response = owner.delete("/api/devices/不存在")

    assert response.status_code == 404


def test_revoke_same_device_twice_is_404(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner, relay_env.client() as second:
        pair_and_device_id(relay_env, owner)
        second_id = pair_and_device_id(relay_env, second)
        first = owner.delete(f"/api/devices/{second_id}")
        second_attempt = owner.delete(f"/api/devices/{second_id}")

    assert first.status_code == 204
    assert second_attempt.status_code == 404


def test_revoke_other_devices_keeps_current_one(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner, relay_env.client() as b, relay_env.client() as c:
        owner_id = pair_and_device_id(relay_env, owner)
        pair_and_device_id(relay_env, b)
        pair_and_device_id(relay_env, c)

        response = owner.delete("/api/devices")
        survivors = owner.get("/api/devices").json()
        b_after = b.get("/api/devices")
        c_after = c.get("/api/devices")

    assert response.status_code == 200
    assert response.json()["revoked"] == 2
    assert [device["id"] for device in survivors["devices"]] == [owner_id]
    assert b_after.status_code == 401
    assert c_after.status_code == 401


def test_revoke_other_devices_with_nothing_to_revoke(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner:
        pair_and_device_id(relay_env, owner)
        response = owner.delete("/api/devices")

    assert response.status_code == 200
    assert response.json()["revoked"] == 0


# ---- 重置主人链接 ----


def test_rotate_invalidates_old_link_but_keeps_paired_devices(relay_env):
    relay_env.add_room()
    old_secret = relay_env.room.owner_secret

    with relay_env.client() as owner:
        device_id = pair_and_device_id(relay_env, owner)
        rotated = owner.post("/api/owner-secret/rotate")
        assert rotated.status_code == 200
        new_path = rotated.json()["pairPath"]

        # 已配对设备不受影响
        still_authorized = owner.get("/api/devices")
        # 旧链接立即失效
        old_link = owner.get(f"/pair/{old_secret}")
        # 当前设备不能因为重置而被踢出去
        current = owner.get("/api/devices").json()["currentDeviceId"]

    assert new_path.startswith("/pair/")
    assert new_path != f"/pair/{old_secret}"
    assert old_link.status_code == 404
    assert still_authorized.status_code == 200
    assert current == device_id


def test_new_link_after_rotation_works(relay_env):
    relay_env.add_room()
    with relay_env.client() as owner:
        pair_and_device_id(relay_env, owner)
        rotated = owner.post("/api/owner-secret/rotate").json()

    secret = rotated["pairPath"].removeprefix("/pair/")
    with relay_env.client() as fresh:
        response = fresh.get(f"/pair/{secret}")

    assert response.status_code == 303
    assert len(relay_env.read("SELECT id FROM devices")) == 2


def test_guest_cannot_rotate_owner_secret(relay_env):
    relay_env.add_room()
    guest = relay_env.add_guest_device()
    with relay_env.cookie_client(guest.token) as guest_client:
        response = guest_client.post("/api/owner-secret/rotate")

    assert response.status_code == 403
    assert relay_env.read("SELECT COUNT(*) AS n FROM rooms") == [{"n": 1}]


def test_rotate_requires_authentication(relay_env):
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.post("/api/owner-secret/rotate")

    assert response.status_code == 401
