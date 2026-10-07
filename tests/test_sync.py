import time
from pathlib import Path

import pytest

from chattrace.storage import Storage
from chattrace.sync_service import SyncService, history_cutoff
from chattrace.wechat_adapter import WeChatStatus

from fakes import OK_STATUS, SELF, FakeReader, FakeShards, make_settings


def direct_rows():
    return [
        {"local_id": 1, "sort_seq": 1, "create_time": 100, "sender": "u1", "content": "第一条"},
        {"local_id": 2, "sort_seq": 2, "create_time": 101, "sender": "u1", "local_type": 3, "content": "[图片]"},
        {"local_id": 3, "sort_seq": 3, "create_time": 102, "sender": SELF, "content": "第二条"},
    ]


def make_service(tmp_path: Path, conversations=None, *, username="chat", status=OK_STATUS):
    settings = make_settings(tmp_path)
    storage = Storage(settings.db_path)
    storage.initialize()
    storage.upsert_conversations([{"username": username, "display_name": "Chat", "last_time": 102}])
    conversation = storage.list_conversations()[0]
    storage.configure_conversation(
        conversation["id"], enabled=True, history_mode="all", history_start_at=None
    )
    reader = FakeReader(conversations or {username: FakeShards(username, direct_rows(), shards=1)},
                        nicknames={"u1": "张三"})
    service = SyncService(storage, settings, reader_factory=lambda: reader, status_check=lambda: status)
    return storage, service, reader, conversation


def all_messages(storage, query=None):
    return storage.query_messages(
        conversation_ids=None, start_at=None, end_at=None, query=query,
        order="asc", limit=500, offset=0,
    )


def job(storage, job_id):
    return next(item for item in storage.list_jobs() if item["id"] == job_id)


def test_sync_is_text_only_and_idempotent(tmp_path: Path):
    storage, service, reader, _ = make_service(tmp_path)
    first = job(storage, service.start(wait=True))
    assert first["status"] == "success"
    assert (first["total_count"], first["scanned_count"]) == (3, 3)
    assert first["inserted_count"] == 2
    assert first["skipped_count"] == 1
    total, rows = all_messages(storage)
    assert total == 2
    assert [row["content"] for row in rows] == ["第一条", "第二条"]
    assert [(row["sender_display_name"], row["is_self"]) for row in rows] == [("张三", 0), (SELF, 1)]
    assert reader.closed == 1

    service.start(wait=True)
    assert all_messages(storage)[0] == 2


def test_sync_updates_changed_content_without_duplicate(tmp_path: Path):
    storage, service, reader, _ = make_service(tmp_path)
    service.start(wait=True)
    reader.conversations["chat"].rows[0]["content"] = "第一条（修订）"
    second = job(storage, service.start(wait=True))
    assert second["updated_count"] == 1
    assert all_messages(storage, "修订")[0] == 1
    assert all_messages(storage)[0] == 2


def test_group_prefix_repaired_on_resync(tmp_path: Path):
    """A v1 row stored with the raw "wxid:\\n" prefix is fixed in place."""
    rows = [
        {"local_id": 1, "sort_seq": 10, "create_time": 100, "sender": "wxid_a", "content": "wxid_a:\n大家好"},
        {"local_id": 2, "sort_seq": 11, "create_time": 101, "sender": SELF, "content": "收到"},
    ]
    storage, service, _, _ = make_service(
        tmp_path, {"g@chatroom": FakeShards("g@chatroom", rows)}, username="g@chatroom"
    )
    service.start(wait=True)
    _, stored = all_messages(storage)
    assert [(r["content"], r["sender_source_id"], r["is_self"]) for r in stored] == [
        ("大家好", "wxid_a", 0),
        ("收到", SELF, 1),
    ]


def test_cursor_advances_over_non_text(tmp_path: Path):
    rows = [{"local_id": 1, "sort_seq": 5, "create_time": 100, "sender": "u1", "local_type": 3, "content": "[图片]"}]
    storage, service, _, conversation = make_service(
        tmp_path, {"chat": FakeShards("chat", rows, shards=1)}
    )
    service.start(wait=True)
    with storage.connect() as conn:
        cursor = conn.execute(
            "SELECT * FROM sync_cursors WHERE conversation_id=?", (conversation["id"],)
        ).fetchone()
    assert (cursor["last_sort_seq"], cursor["last_message_at"]) == (5, 100)


def test_incremental_reads_only_new_and_lookback(tmp_path: Path):
    day = 24 * 60 * 60
    base = 1_700_000_000
    rows = [
        {"local_id": i, "sort_seq": i, "create_time": base + i * 2 * day, "sender": "u1", "content": f"m{i}"}
        for i in range(1, 6)
    ]
    shards = FakeShards("chat", rows, shards=1)
    storage, service, _, _ = make_service(tmp_path, {"chat": shards})
    service.start(wait=True)
    shards.rows.append({"local_id": 6, "sort_seq": 6, "create_time": base + 12 * day, "sender": "u1", "content": "m6"})
    second = job(storage, service.start(wait=True))
    # m6 is new; m5 falls inside the 24h lookback before the last message.
    assert second["scanned_count"] == 2
    assert second["inserted_count"] == 1


def test_recent_mode_limits_history(tmp_path: Path):
    now = int(time.time())
    rows = [
        {"local_id": 1, "sort_seq": 1, "create_time": now - 100 * 86400, "sender": "u1", "content": "old"},
        {"local_id": 2, "sort_seq": 2, "create_time": now - 10 * 86400, "sender": "u1", "content": "new"},
    ]
    storage, service, _, conversation = make_service(tmp_path, {"chat": FakeShards("chat", rows, shards=1)})
    storage.configure_conversation(
        conversation["id"], enabled=True, history_mode="recent_30d", history_start_at=None
    )
    service.start(wait=True)
    assert [r["content"] for r in all_messages(storage)[1]] == ["new"]


def test_history_change_resets_cursor_and_backfills(tmp_path: Path):
    now = int(time.time())
    rows = [
        {"local_id": 1, "sort_seq": 1, "create_time": now - 100 * 86400, "sender": "u1", "content": "old"},
        {"local_id": 2, "sort_seq": 2, "create_time": now - 10 * 86400, "sender": "u1", "content": "new"},
    ]
    storage, service, _, conversation = make_service(tmp_path, {"chat": FakeShards("chat", rows, shards=1)})
    cid = conversation["id"]
    storage.configure_conversation(cid, enabled=True, history_mode="recent_30d", history_start_at=None)
    service.start(wait=True)
    storage.configure_conversation(cid, enabled=True, history_mode="all", history_start_at=None)
    service.start(wait=True)
    assert all_messages(storage)[0] == 2


def test_history_cutoff_modes():
    now = 1_000_000_000
    assert history_cutoff({"history_mode": "all"}, now) == 0
    assert history_cutoff({"history_mode": "since", "history_start_at": 123}, now) == 123
    assert history_cutoff({"history_mode": "recent_90d"}, now) == now - 90 * 86400


def test_start_refuses_when_wechat_unavailable(tmp_path: Path):
    blocked = WeChatStatus(False, False, None, None, False, False, "微信未运行")
    storage, service, _, _ = make_service(tmp_path, status=blocked)
    with pytest.raises(ValueError, match="微信未运行"):
        service.start(wait=True)
    assert storage.list_jobs() == []
    # The lock must be released after a refused start.
    service.status_check = lambda: OK_STATUS
    assert job(storage, service.start(wait=True))["status"] == "success"


def test_cancel_stops_job(tmp_path: Path):
    rows = [
        {"local_id": i, "sort_seq": i, "create_time": 100 + i, "sender": "u1", "content": f"m{i}"}
        for i in range(1, 1200)
    ]
    storage, service, reader, _ = make_service(tmp_path, {"chat": FakeShards("chat", rows, shards=1)})
    original = reader.open_conversation

    def cancelling_open(username):
        service.cancel(storage.active_job_id())
        return original(username)

    reader.open_conversation = cancelling_open
    result = job(storage, service.start(wait=True))
    assert result["status"] == "cancelled"
    assert reader.closed == 1


def test_interrupted_jobs_are_recovered(tmp_path: Path):
    storage, _, _, conversation = make_service(tmp_path)
    job_id = storage.create_sync_job("manual", [conversation])
    with storage.connect() as conn:
        conn.execute("UPDATE sync_jobs SET status='running' WHERE id=?", (job_id,))
    assert storage.active_job_id() == job_id
    assert storage.recover_interrupted_jobs() == 1
    assert storage.active_job_id() is None
    assert job(storage, job_id)["status"] == "interrupted"
