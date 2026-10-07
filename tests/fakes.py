from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from chattrace.config import Settings
from chattrace.wechat_adapter import ConversationSource, WeChatStatus


SELF = "wxid_self"
OK_STATUS = WeChatStatus(True, True, "4.1.15.13", None, True, True, "ok")


def msg_table(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


class FakeShards:
    """In-memory imitation of WeChat 4.x message shards.

    Each shard has its own Name2Id table, like the real message_N.db files.
    Rows are dicts with: shard, local_id, local_type, sender, create_time,
    sort_seq, content, server_id.
    """

    def __init__(self, username: str, rows: list[dict], shards: int = 2):
        self.username = username
        self.rows = rows
        self.shards = shards

    def connect(self) -> list[tuple[sqlite3.Connection, str]]:
        table = msg_table(self.username)
        tables = []
        for shard in range(self.shards):
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute(
                f"CREATE TABLE {table} (local_id INTEGER PRIMARY KEY, local_type INTEGER,"
                " real_sender_id INTEGER, create_time INTEGER, message_content,"
                " compress_content, server_id INTEGER, sort_seq INTEGER)"
            )
            ids: dict[str, int] = {}
            for row in self.rows:
                if row.get("shard", 0) != shard:
                    continue
                sender = row["sender"]
                if sender not in ids:
                    ids[sender] = conn.execute(
                        "INSERT INTO Name2Id(user_name) VALUES(?)", (sender,)
                    ).lastrowid
                conn.execute(
                    f"INSERT INTO {table} VALUES(?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        row["local_id"],
                        row.get("local_type", 1),
                        ids[sender],
                        row["create_time"],
                        row["content"],
                        row.get("server_id", 0),
                        row["sort_seq"],
                    ),
                )
            if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                tables.append((conn, table))
        return tables


class FakeReader:
    def __init__(self, conversations: dict[str, FakeShards], nicknames: dict[str, str] | None = None):
        self.conversations = conversations
        self._nicknames = nicknames or {}
        self.closed = 0

    def nicknames(self):
        return self._nicknames

    @contextmanager
    def open_conversation(self, username: str):
        shards = self.conversations.get(username)
        if shards is None:
            yield None
            return
        tables = shards.connect()
        try:
            yield ConversationSource(tables, username=username, self_username=SELF)
        finally:
            for conn, _ in tables:
                conn.close()

    def close(self):
        self.closed += 1


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "db.sqlite",
        wechat_cache_dir=tmp_path / "cache",
        compatible_versions=frozenset({"4.1.15.13"}),
    )
