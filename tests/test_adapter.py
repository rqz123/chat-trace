import random

import pytest

from chattrace.wechat_adapter import (
    ConversationSource,
    MessageWindow,
    read_key_file,
    seal_key_file,
    split_group_content,
    unnamed_group_title,
    win32crypt,
    write_key_file,
)

from fakes import SELF, FakeShards


def test_split_group_content_strips_matching_prefix():
    assert split_group_content("wxid_abc:\n你好", "wxid_abc") == ("你好", "wxid_abc")
    # Sender unknown but prefix looks like a WeChat username.
    assert split_group_content("zhangsan_01:\n在吗", None) == ("在吗", "zhangsan_01")


def test_split_group_content_keeps_user_text():
    assert split_group_content("备注:\n明天开会", "wxid_abc") == ("备注:\n明天开会", None)
    assert split_group_content("没有前缀", "wxid_abc") == ("没有前缀", None)
    # Username-like text from a *known, different* sender is body, not a prefix.
    assert split_group_content("zhangsan_01:\nbody", "wxid_actual_sender") == (
        "zhangsan_01:\nbody",
        None,
    )
    # Not username-like and sender unknown: keep.
    assert split_group_content("会议 纪要:\n内容", None) == ("会议 纪要:\n内容", None)


def _group_rows(count: int) -> list[dict]:
    rng = random.Random(7)
    rows = []
    for i in range(count):
        sender = rng.choice(["wxid_a", "wxid_b", SELF])
        text = f"消息{i}"
        rows.append(
            {
                "shard": i % 2,
                "local_id": i // 2 + 1,
                "sender": sender,
                "create_time": 1_700_000_000 + i,
                # Frequent ties exercise the boundary handling.
                "sort_seq": 1000 + i // 3,
                "content": text if sender == SELF else f"{sender}:\n{text}",
            }
        )
    return rows


def test_keyset_pages_cover_every_row_exactly_once():
    shards = FakeShards("123@chatroom", _group_rows(257))
    tables = shards.connect()
    source = ConversationSource(tables, username="123@chatroom", self_username=SELF)
    seen = []
    for batch in source.batches(MessageWindow(), batch_size=10):
        seen.extend(batch)
    keys = [(m["sort_seq"], m["local_id"], m["content"]) for m in seen]
    assert len(keys) == 257 == len(set(keys))
    seqs = [m["sort_seq"] for m in seen]
    assert seqs == sorted(seqs, reverse=True)
    assert source.count(MessageWindow()) == 257


def test_group_sender_resolved_from_shard_name2id():
    shards = FakeShards("123@chatroom", _group_rows(30))
    source = ConversationSource(shards.connect(), username="123@chatroom", self_username=SELF)
    messages = [m for batch in source.batches(MessageWindow(), 100) for m in batch]
    for message in messages:
        assert ":\n" not in message["content"]
        assert message["sender_username"] in {"wxid_a", "wxid_b", SELF}
        assert message["is_self"] == (message["sender_username"] == SELF)


def test_window_filters_match_count():
    shards = FakeShards("123@chatroom", _group_rows(90))
    source = ConversationSource(shards.connect(), username="123@chatroom", self_username=SELF)
    window = MessageWindow(history_cutoff=1_700_000_030, after_sort_seq=1020, lookback_from=1_700_000_080)
    rows = [m for batch in source.batches(window, 7) for m in batch]
    assert len(rows) == source.count(window)
    assert all(
        m["create_time"] >= 1_700_000_030 and (m["sort_seq"] > 1020 or m["create_time"] >= 1_700_000_080)
        for m in rows
    )


@pytest.mark.skipif(win32crypt is None, reason="DPAPI is Windows-only")
def test_key_file_is_sealed_with_dpapi(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text('{"message\\\\message_0.db": "00ff"}', encoding="utf-8")
    (tmp_path / "keys.json.bak").write_text('{"contact\\\\contact.db": "aa"}', encoding="utf-8")
    assert seal_key_file(path) is True
    raw = path.read_bytes()
    assert b"00ff" not in raw and b"message_0" not in raw
    assert not (tmp_path / "keys.json.bak").exists()
    assert read_key_file(path) == {"message\\message_0.db": b"\x00\xff", "contact\\contact.db": b"\xaa"}
    assert seal_key_file(path) is False

    write_key_file(path, {"x": b"\x01"})
    assert read_key_file(path) == {"x": b"\x01"}


def test_unnamed_group_title_matches_wechat():
    members = [
        {"username": SELF, "nick_name": "我"},
        {"username": "wxid_d", "nick_name": "张凝"},
        {"username": "wxid_b", "nick_name": "何金水", "remark": "何金水- IT"},
        {"username": "wxid_a", "nick_name": "Siqu Liu"},
        {"username": "wxid_c", "nick_name": "汪洋"},
    ]
    assert unnamed_group_title(members, SELF) == "Siqu Liu、何金水- IT、汪洋、张凝 (5)"
    more = members + [{"username": "wxid_e", "nick_name": "杨铮"}]
    assert unnamed_group_title(more, SELF) == "Siqu Liu、何金水- IT、汪洋、张凝… (6)"
    # Only the account itself: no usable title, the group keeps its id.
    assert unnamed_group_title(members[:1], SELF) == ""


class _Proc:
    def __init__(self, pid, parent):
        self.pid, self._parent = pid, parent

    def ppid(self):
        return self._parent


def test_login_state_from_weixin_process_tree(monkeypatch):
    import chattrace.wechat_adapter as adapter

    monkeypatch.setattr(adapter, "_logged_in_main_pid", None)
    login_window = [_Proc(10, 1)]
    logged_in = [_Proc(10, 1), _Proc(11, 10), _Proc(12, 10)]
    assert adapter.login_state(login_window) == (False, False)
    assert adapter.login_state(logged_in) == (True, False)
    # Helpers exit on logout while the main process stays.
    assert adapter.login_state(login_window) == (False, True)
    # A restarted WeChat at the login window has simply not logged in.
    assert adapter.login_state([_Proc(20, 1)]) == (False, False)
    assert adapter.login_state(login_window) == (False, False)
