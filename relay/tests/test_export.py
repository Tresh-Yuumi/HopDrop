"""区域导出（方案 4.1、9.3）。

导出是**只读**操作，因此这批用例的断言重心不在状态变更，而在三处容易错、
错了又不会报错的地方：

1. **顺序**。分页与快照倒序，导出正序——两个方向各自成立，混起来不会抛异常，
   只会让导出文件从最后一条开始读。
2. **排序键要带 id 兜底**。`created_at` 秒级精度，同一秒写入的多条消息若无
   次级键，两次导出可能给出不同顺序。
3. **软删除的消息不出现**，但回收站里的东西仍在库中——所以这条必须靠断言
   正文内容来验，而不是靠条数。
"""

from __future__ import annotations

import time
from urllib.parse import unquote

import pytest
from conftest import RelayEnv


def _create_board(client, name: str, retention: int = 0) -> dict:
    response = client.post("/api/boards", json={"name": name, "retention": retention})
    assert response.status_code == 201, response.text
    return response.json()["board"]


def _create_note(client, board_id: str, content: str) -> dict:
    response = client.post(
        f"/api/boards/{board_id}/notes",
        json={"content": content, "mutationId": f"m-{time.time_ns()}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["note"]


def _export(client, board_id: str, fmt: str = "txt"):
    return client.get(f"/api/boards/{board_id}/export", params={"format": fmt})


# ------------------------------------------------------------------ 基本契约


def test_export_requires_session(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.client() as client:
        response = client.get("/api/boards/whatever/export")

    assert response.status_code == 401


def test_export_defaults_to_text(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "第一条")

        response = _export(client, board["id"])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].startswith("attachment;")


def test_export_as_markdown(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "第一条")

        response = _export(client, board["id"], "md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# 工作")


def test_export_rejects_unknown_format_without_falling_back(relay_env: RelayEnv) -> None:
    """传 `pdf` 时不能悄悄回落到 txt——调用方会以为自己拿到了 PDF。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        response = _export(client, board["id"], "pdf")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_export_format"


def test_export_unknown_board_is_404(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = _export(client, "no-such-board")

    assert response.status_code == 404


def test_guest_cannot_export_owner_board(relay_env: RelayEnv) -> None:
    """访客看不到主人的区域，导出也不该看得到——否则导出就成了绕过可见性的
    读取通道。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")

    guest = relay_env.add_guest_device()
    with relay_env.cookie_client(guest.token) as client:
        response = _export(client, board["id"])

    assert response.status_code == 404


def test_guest_can_export_guest_board(relay_env: RelayEnv) -> None:
    """访客区的导出对访客开放：他本来就能读到那些内容。"""
    relay_env.add_room()
    guest = relay_env.add_guest_device()
    with relay_env.cookie_client(guest.token) as client:
        boards = client.get("/api/boards").json()["boards"]
    guest_board = next(b for b in boards if b["isGuest"])
    with relay_env.cookie_client(guest.token) as client:
        _create_note(client, guest_board["id"], "访客写的一条")
        response = _export(client, guest_board["id"])

    assert response.status_code == 200
    assert "访客写的一条" in response.text


# ------------------------------------------------------------------ 内容


def test_export_is_chronological(relay_env: RelayEnv) -> None:
    """导出是**正序**，与分页/快照相反。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        for index in range(4):
            _create_note(client, board["id"], f"第{index}条")

        response = _export(client, board["id"])

    body = response.text
    positions = [body.index(f"第{index}条") for index in range(4)]
    assert positions == sorted(positions)


def test_export_orders_within_the_same_second_by_insertion(relay_env: RelayEnv) -> None:
    """同一秒写入的多条消息必须按**写入顺序**导出。

    这四条消息的 `created_at` 完全相同（都是这一秒），所以排序完全由次级键
    决定。次级键若是 `id`（随机 hex），这里的顺序就是随机的——这个用例正是
    为了钉住"次级键必须是 rowid"这件事。四条一起测是因为随机顺序碰巧正确的
    概率在 4 条时还有 1/24；数量越大越能暴露问题。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        contents = [f"第{index}条" for index in range(8)]
        for content in contents:
            _create_note(client, board["id"], content)

        response = _export(client, board["id"])

    # 确认前提成立：它们真的落在同一秒里，否则这条用例什么都没验到
    stamps = {row["created_at"] for row in relay_env.read("SELECT created_at FROM notes")}
    assert len(stamps) == 1, "这些消息没有落在同一秒，用例失去意义"

    body = response.text
    assert [body.index(c) for c in contents] == sorted(body.index(c) for c in contents)


def test_export_contains_header_metadata(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作", retention=30)
        _create_note(client, board["id"], "内容")
        response = _export(client, board["id"])

    body = response.text
    assert "HopDrop 区域导出" in body
    assert "区域：工作" in body
    assert "保留期：30 天" in body
    assert "消息数：1" in body
    assert "导出时间：" in body


def test_export_marks_permanent_retention(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作", retention=0)
        _create_note(client, board["id"], "内容")
        response = _export(client, board["id"])

    assert "保留期：永久" in response.text


def test_export_includes_author_name(relay_env: RelayEnv) -> None:
    """每条消息的元信息行带作者名。

    `paired_client` 用 TestClient，它不发 User-Agent，所以设备名落到默认值
    "新设备"——这里断言具体值而不是"含某个字"，否则这条用例对"作者名取错
    了列"这类回归是无效的。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "内容")
        response = _export(client, board["id"])

    meta = next(line for line in response.text.splitlines() if line.startswith("["))
    assert meta.endswith("] 新设备")


def test_export_marks_pinned_and_edited(relay_env: RelayEnv) -> None:
    """标记写在元信息行的括号里，两条并存时用顿号连接。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        pinned = _create_note(client, board["id"], "置顶的")
        _create_note(client, board["id"], "普通的一条")
        assert client.patch(f"/api/notes/{pinned['id']}", json={"pinned": True}).status_code == 200
        response = _export(client, board["id"])

    body = response.text
    # 只有被置顶的那条带标记，且标记写在时间与作者之后
    marked = [line for line in body.splitlines() if line.startswith("[") and "（" in line]
    assert len(marked) == 1
    assert marked[0].endswith("（置顶）")
    # 未编辑过的消息不该出现"已编辑"
    assert "已编辑" not in body


def test_export_marks_edited_note(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        note = _create_note(client, board["id"], "初稿")
        assert client.patch(f"/api/notes/{note['id']}", json={"content": "改过"}).status_code == 200
        response = _export(client, board["id"])

    body = response.text
    assert "（已编辑）" in body
    assert "改过" in body


def test_export_marks_file_message(relay_env: RelayEnv) -> None:
    """文件消息（M7）在导出里标出来并给一句说明，而不是留空。

    这里直接把一条 `kind='file_ref'` 的消息插进库——文件上传接口在 M7，但
    导出对它的处理现在就该是对的，否则 M7 接进来时会一起改到这里。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "普通文本")

    relay_env.execute(
        """
        INSERT INTO notes
          (id, room_id, board_id, author_id, kind, content, pinned, mutation_id,
           deleted_at, created_at, updated_at)
        VALUES ('note-file-1', ?, ?, NULL, 'file_ref', '文件：报告.pdf', 0,
                'm-file-1', NULL, ?, ?)
        """,
        (relay_env.room.room_id, board["id"], int(time.time()), int(time.time())),
    )

    with relay_env.paired_client() as client:
        response = _export(client, board["id"])

    body = response.text
    assert "（文件消息）" in body
    assert "（文件消息，请到应用页查看）" in body
    assert "文件：报告.pdf" not in body  # 占位正文不能被当成用户内容输出


def test_export_excludes_deleted_notes(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "保留的")
        doomed = _create_note(client, board["id"], "删掉的")
        assert client.delete(f"/api/notes/{doomed['id']}").status_code == 204
        response = _export(client, board["id"])

    body = response.text
    assert "保留的" in body
    assert "删掉的" not in body
    assert "消息数：1" in body


def test_export_ignores_notes_from_other_boards(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        first = _create_board(client, "甲")
        second = _create_board(client, "乙")
        _create_note(client, first["id"], "甲的内容")
        _create_note(client, second["id"], "乙的内容")
        response = _export(client, first["id"])

    assert "甲的内容" in response.text
    assert "乙的内容" not in response.text


def test_export_can_be_exported_when_archived(relay_env: RelayEnv) -> None:
    """方案 4.2：归档区只能导出和恢复——所以导出必须对归档区开放。

    归档**动作**本身属于生命周期（M9），M5 尚未提供接口，所以这里直接改库
    把状态摆到"已归档"。要验的是导出这条读路径不看 `status`，与谁把状态改成
    归档无关。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "归档前的内容")

    relay_env.execute(
        "UPDATE boards SET status = 'archived', archived_at = ? WHERE id = ?",
        (int(time.time()), board["id"]),
    )

    with relay_env.paired_client() as client:
        response = _export(client, board["id"])

    assert response.status_code == 200
    assert "归档前的内容" in response.text


def test_export_of_empty_board_succeeds(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "空区域")
        response = _export(client, board["id"])

    assert response.status_code == 200
    assert "消息数：0" in response.text


def test_export_is_utf8(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "中文与 emoji 🚀")
        response = _export(client, board["id"])

    assert "中文与 emoji 🚀" in response.text
    assert response.content.decode("utf-8")


def test_export_does_not_change_rev(relay_env: RelayEnv) -> None:
    """导出是只读的：拉一次导出不该让任何客户端去拉快照。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作")
        _create_note(client, board["id"], "内容")
        before = relay_env.room_rev()
        _export(client, board["id"])
        _export(client, board["id"], "md")
        after = relay_env.room_rev()

    assert before == after


# ------------------------------------------------------------------ 文件名


def test_filename_uses_rfc5987_for_chinese(relay_env: RelayEnv) -> None:
    """中文文件名不能直接放进 `filename="..."` 那个位置（RFC 只允许 ASCII）。"""
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作笔记")
        response = _export(client, board["id"])

    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "工作笔记" not in disposition  # 裸中文不能出现
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    assert "工作笔记" in unquote(encoded)
    assert disposition.endswith(".txt")


def test_ascii_fallback_filename_is_ascii_only(relay_env: RelayEnv) -> None:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, "工作笔记")
        response = _export(client, board["id"], "md")

    disposition = response.headers["content-disposition"]
    fallback = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert fallback.isascii()
    assert fallback.endswith(".md")


@pytest.mark.parametrize("hostile", ['a/b\\c:d*e?f"g<h>i|j', "  ..dots..  "])
def test_hostile_board_name_yields_safe_filename(relay_env: RelayEnv, hostile: str) -> None:
    """Windows 与多数文件系统不接受这些字符。不处理的话，下载下来就是一个
    落不了盘的文件名。

    控制字符那一路走不到这里——`create_board` 在改名阶段就用 `invalid_board_name`
    拒了。所以下面单独有一条针对 `safe_filename_stem` 的单元测试覆盖它，
    而不是在这里造一个建不出来的区域。
    """
    relay_env.add_room()
    with relay_env.paired_client() as client:
        board = _create_board(client, hostile)
        response = _export(client, board["id"])

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    name = unquote(encoded)
    for char in '\\/:*?"<>|':
        assert char not in name, f"文件名里残留了 {char!r}：{name}"
    assert name.endswith(".txt")
    # 全空白的名要回落到默认主干，而不是得到一个只剩时间戳的文件名
    assert not name.startswith("-")


def test_filename_stem_guards_length_and_control_chars() -> None:
    """`safe_filename_stem` 的两条兜底直接测。

    它们**在接口层不可达**（区域名上限 64 字符、且不允许控制字符），但函数
    存在就是为了兜住"以后上限改了"或"别处也来调它"。不可达的分支最容易被
    改坏而没人发现，所以按单元测。
    """
    from app.export import safe_filename_stem

    assert safe_filename_stem("a\x01b\x7fc") == "a_b_c"
    assert len(safe_filename_stem("长" * 300)) == 80
    assert safe_filename_stem("") == "board"
    assert safe_filename_stem("...") == "board"
    assert safe_filename_stem("   ") == "board"
    assert safe_filename_stem("工作") == "工作"
