import sqlite3
from pathlib import Path

from chattrace.storage import SCHEMA, Storage


def _storage_with_messages(tmp_path: Path, contents: list[str]) -> tuple[Storage, int]:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.upsert_conversations([{"username": "a", "display_name": "A", "last_time": 123}])
    conversation_id = storage.list_conversations()[0]["id"]
    with storage.connect() as conn:
        for index, content in enumerate(contents):
            conn.execute(
                """
                INSERT INTO messages(
                    conversation_id, source_key, sort_seq, message_type, content,
                    content_hash, source_created_at, archived_at, updated_at
                ) VALUES(?, ?, ?, 'text', ?, 'h', ?, 'n', 'n')
                """,
                (conversation_id, f"k{index}", index, content, 100 + index),
            )
    return storage, conversation_id


def _search(storage: Storage, query: str, **kwargs) -> tuple[int, list]:
    params = dict(conversation_ids=None, start_at=None, end_at=None, order="desc", limit=20, offset=0)
    params.update(kwargs)
    return storage.query_messages(query=query, **params)


def test_conversations_default_to_not_selected(tmp_path: Path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.upsert_conversations(
        [{"username": "a", "display_name": "A", "last_time": 123}]
    )
    conversations = storage.list_conversations()
    assert len(conversations) == 1
    assert conversations[0]["sync_enabled"] == 0
    assert storage.selected_conversations() == []


def test_query_messages_filters_content(tmp_path: Path):
    storage, conversation_id = _storage_with_messages(tmp_path, ["项目 Alpha 进展"])
    total, rows = _search(storage, "Alpha", conversation_ids=[conversation_id], start_at=1, end_at=200)
    assert total == 1
    assert rows[0]["content"] == "项目 Alpha 进展"


def test_search_uses_fts_and_short_terms(tmp_path: Path):
    storage, _ = _storage_with_messages(
        tmp_path, ["明天下午三点开会", "会议改到周五", "100% 完成", "下午茶"]
    )
    assert storage.fts_enabled
    assert _search(storage, "下午三点")[0] == 1          # FTS (>=3 chars)
    assert _search(storage, "会")[0] == 2                # LIKE (1 char)
    assert _search(storage, "下午 开会")[0] == 1         # AND of terms
    assert _search(storage, "100%")[0] == 1              # FTS with symbols
    assert _search(storage, '"引号"')[0] == 0            # quotes are escaped


def test_fts_follows_updates_and_deletes(tmp_path: Path):
    storage, _ = _storage_with_messages(tmp_path, ["原始的内容文本"])
    with storage.connect() as conn:
        conn.execute("UPDATE messages SET content='修订后的文本'")
    assert _search(storage, "原始的")[0] == 0
    assert _search(storage, "修订后")[0] == 1
    with storage.connect() as conn:
        conn.execute("DELETE FROM messages")
    assert _search(storage, "修订后")[0] == 0


def test_v1_database_is_migrated(tmp_path: Path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("CREATE TABLE summaries (id INTEGER PRIMARY KEY)")  # unused v1 table
    conn.execute("INSERT INTO app_meta VALUES('schema_version', '1')")
    conn.execute(
        "INSERT INTO conversations(source_username, display_name, created_at, updated_at)"
        " VALUES('g@chatroom', 'G', 'n', 'n')"
    )
    conn.execute(
        "INSERT INTO messages(conversation_id, source_key, sort_seq, content, content_hash,"
        " source_created_at, archived_at, updated_at) VALUES(1, 'k', 1, '旧版本消息内容', 'h', 1, 'n', 'n')"
    )
    conn.execute("INSERT INTO sync_cursors(conversation_id, last_sort_seq, updated_at) VALUES(1, 99, 'n')")
    conn.commit()
    conn.close()

    storage = Storage(path)
    storage.initialize()
    with storage.connect() as conn:
        assert conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0] == "4"
        assert conn.execute("SELECT COUNT(*) FROM sync_cursors").fetchone()[0] == 0
        # v3 drops the unused, empty v1 "summaries" table.
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='summaries'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='topic_summaries'").fetchone()
    # Existing rows are indexed by the FTS rebuild.
    assert _search(storage, "旧版本")[0] == 1

    # Re-initializing a v2 database does not reset cursors again.
    with storage.connect() as conn:
        conn.execute("INSERT INTO sync_cursors(conversation_id, last_sort_seq, updated_at) VALUES(1, 5, 'n')")
    storage.initialize()
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_cursors").fetchone()[0] == 1


def test_untracked_conversations(tmp_path: Path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    now = 1_800_000_000
    storage.upsert_conversations([
        {"username": "old", "display_name": "Old", "last_time": now - 400 * 86400},
        {"username": "old_selected", "display_name": "OldSel", "last_time": now - 400 * 86400},
        {"username": "recent", "display_name": "Recent", "last_time": now - 86400},
        {"username": "never", "display_name": "Never"},
    ])
    ids = {c["source_username"]: c["id"] for c in storage.list_conversations()}
    storage.configure_conversation(ids["old_selected"], enabled=True, history_mode="all", history_start_at=None)

    # Bulk: inactive for a year, never touching selected conversations.
    cutoff = now - 365 * 86400
    assert storage.count_inactive_conversations(cutoff) == 2  # old + never (no timestamp)
    assert storage.hide_inactive_conversations(cutoff) == 2
    hidden = {c["source_username"] for c in storage.list_conversations() if c["hidden"]}
    assert hidden == {"old", "never"}
    assert [c["source_username"] for c in storage.selected_conversations()] == ["old_selected"]

    # A rescan keeps the user's choice.
    storage.upsert_conversations([{"username": "old", "display_name": "Old renamed", "last_time": now}])
    assert next(c for c in storage.list_conversations() if c["source_username"] == "old")["hidden"] == 1

    # Untracking a selected conversation stops its sync; restoring does not restart it.
    assert storage.set_conversations_hidden([ids["old_selected"]], True) == 1
    assert storage.selected_conversations() == []
    storage.set_conversations_hidden([ids["old_selected"]], False)
    restored = next(c for c in storage.list_conversations() if c["id"] == ids["old_selected"])
    assert (restored["hidden"], restored["sync_enabled"]) == (0, 0)

    # Choosing to sync a hidden conversation brings it back.
    storage.configure_conversation(ids["old"], enabled=True, history_mode="all", history_start_at=None)
    assert next(c for c in storage.list_conversations() if c["id"] == ids["old"])["hidden"] == 0


def test_unnamed_groups_are_auto_untracked(tmp_path: Path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.upsert_conversations([
        {"username": "111@chatroom", "display_name": "111@chatroom"},   # no name
        {"username": "222@chatroom", "display_name": ""},                # no name
        {"username": "333@chatroom", "display_name": "项目群"},           # named group
        {"username": "wxid_a", "display_name": "wxid_a"},                # direct chat: untouched
        {"username": "444@chatroom", "display_name": "444@chatroom"},   # selected below
    ])
    rows = {c["source_username"]: c for c in storage.list_conversations()}
    assert {u for u, c in rows.items() if c["hidden"]} == {"111@chatroom", "222@chatroom", "444@chatroom"}
    assert rows["111@chatroom"]["hidden_reason"] == "unnamed"

    # A group the user chose to sync is never auto-hidden, and choosing sync restores it.
    storage.configure_conversation(rows["444@chatroom"]["id"], enabled=True, history_mode="all", history_start_at=None)
    storage.upsert_conversations([{"username": "444@chatroom", "display_name": "444@chatroom"}])
    assert [c["source_username"] for c in storage.selected_conversations()] == ["444@chatroom"]

    # Getting a name shows an auto-hidden group again.
    storage.upsert_conversations([{"username": "111@chatroom", "display_name": "新群名"}])
    renamed = next(c for c in storage.list_conversations() if c["source_username"] == "111@chatroom")
    assert (renamed["hidden"], renamed["hidden_reason"]) == (0, None)

    # A group the user restored stays visible on later scans.
    storage.set_conversations_hidden([rows["222@chatroom"]["id"]], False)
    storage.upsert_conversations([{"username": "222@chatroom", "display_name": ""}])
    assert next(c for c in storage.list_conversations() if c["source_username"] == "222@chatroom")["hidden"] == 0

    # A named group the user hid stays hidden even if renamed.
    storage.set_conversations_hidden([rows["333@chatroom"]["id"]], True)
    storage.upsert_conversations([{"username": "333@chatroom", "display_name": "改名了"}])
    assert next(c for c in storage.list_conversations() if c["source_username"] == "333@chatroom")["hidden"] == 1


def test_existing_unnamed_groups_untracked_on_startup(tmp_path: Path):
    path = tmp_path / "old.db"
    storage = Storage(path)
    storage.initialize()
    with storage.connect() as conn:  # rows written before the rule existed
        conn.execute(
            "INSERT INTO conversations(source_username, display_name, conversation_type, created_at, updated_at)"
            " VALUES('555@chatroom', '555@chatroom', 'group', 'n', 'n')"
        )
    Storage(path).initialize()
    assert Storage(path).list_conversations()[0]["hidden"] == 1


def test_v4_merges_messages_archived_on_two_computers(tmp_path: Path):
    from chattrace.wechat_adapter import message_source_key, server_message_key

    storage, cid = _storage_with_messages(tmp_path, [])
    with storage.connect() as conn:
        # Same server message archived on computer A (local_id 59) and B (79),
        # plus one message without a server id.
        for key, local_id, server_id in (("a59", 59, "777"), ("b79", 79, "777"), ("x", 5, None)):
            conn.execute(
                "INSERT INTO messages(conversation_id, source_key, source_local_id, source_server_id,"
                " sort_seq, content, content_hash, source_created_at, archived_at, updated_at)"
                " VALUES(?, ?, ?, ?, 1, '跨电脑消息', 'h', 1, 'n', 'n')",
                (cid, key, local_id, server_id),
            )
        conn.execute(
            "INSERT INTO topic_summaries(conversation_id, range_start, range_end, status, model,"
            " prompt_version, created_at) VALUES(?, 0, 1, 'done', 'm', 'p', 'n')",
            (cid,),
        )
        conn.execute(
            "INSERT INTO summary_topics(summary_id, position, title, question, discussion_json,"
            " conclusions_json, open_questions_json, participants_json, status)"
            " VALUES(1, 0, 't', 'q', '[]', '[]', '[]', '[]', 'open')"
        )
        b_id = conn.execute("SELECT id FROM messages WHERE source_key='b79'").fetchone()[0]
        conn.execute("INSERT INTO summary_topic_sources VALUES(1, ?)", (b_id,))
        conn.execute("UPDATE app_meta SET value='3' WHERE key='schema_version'")

    storage.initialize()
    with storage.connect() as conn:
        rows = conn.execute("SELECT id, source_key FROM messages ORDER BY id").fetchall()
        assert [r["source_key"] for r in rows] == [server_message_key(cid, "777"), "x"]
        assert conn.execute("SELECT message_id FROM summary_topic_sources").fetchone()[0] == rows[0]["id"]
    assert _search(storage, "跨电脑")[0] == 2

    # A later sync on either computer resolves to the merged row.
    for local_id in (59, 79, 1234):
        message = {"server_id": 777, "local_id": local_id, "sender_id": local_id}
        assert message_source_key(cid, message) == rows[0]["source_key"]
