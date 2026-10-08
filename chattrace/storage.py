from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    conversation_type TEXT NOT NULL DEFAULT 'other',
    last_source_message_at INTEGER,
    sync_enabled INTEGER NOT NULL DEFAULT 0 CHECK(sync_enabled IN (0, 1)),
    history_mode TEXT NOT NULL DEFAULT 'all',
    history_start_at INTEGER,
    last_successful_sync_at TEXT,
    archived_message_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL UNIQUE,
    source_local_id INTEGER,
    source_server_id TEXT,
    sort_seq INTEGER NOT NULL,
    sender_source_id TEXT,
    sender_display_name TEXT,
    is_self INTEGER NOT NULL DEFAULT 0 CHECK(is_self IN (0, 1)),
    message_type TEXT NOT NULL DEFAULT 'text',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_created_at INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
ON messages(conversation_id, source_created_at, sort_seq);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(source_created_at);

CREATE TABLE IF NOT EXISTS sync_cursors (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    last_sort_seq INTEGER NOT NULL DEFAULT 0,
    last_message_at INTEGER NOT NULL DEFAULT 0,
    last_full_rescan_at TEXT,
    cursor_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    total_count INTEGER NOT NULL DEFAULT 0,
    scanned_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    scanned_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_job_items_job ON sync_job_items(job_id, conversation_id);

CREATE TABLE IF NOT EXISTS topic_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    range_start INTEGER NOT NULL,
    range_end INTEGER NOT NULL,
    range_label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    window_count INTEGER NOT NULL DEFAULT 0,
    done_windows INTEGER NOT NULL DEFAULT 0,
    cached_windows INTEGER NOT NULL DEFAULT 0,
    overview TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_topic_summaries_conversation
ON topic_summaries(conversation_id, id);

CREATE TABLE IF NOT EXISTS summary_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_id INTEGER NOT NULL REFERENCES topic_summaries(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    discussion_json TEXT NOT NULL,
    conclusions_json TEXT NOT NULL,
    open_questions_json TEXT NOT NULL,
    participants_json TEXT NOT NULL,
    status TEXT NOT NULL,
    first_message_at INTEGER,
    last_message_at INTEGER,
    source_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_summary_topics_summary ON summary_topics(summary_id, position);

CREATE TABLE IF NOT EXISTS summary_topic_sources (
    topic_id INTEGER NOT NULL REFERENCES summary_topics(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    PRIMARY KEY (topic_id, message_id)
);

-- One AI call per message window; identical input (same messages, model and
-- prompt version) is reused instead of calling the API again.
CREATE TABLE IF NOT EXISTS ai_window_cache (
    input_hash TEXT PRIMARY KEY,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    result_json TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


SCHEMA_VERSION = 4

ACTIVE_JOB_STATUSES = ("queued", "running")

logger = logging.getLogger("chattrace.storage")

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

# The trigram tokenizer cannot match terms shorter than three characters.
FTS_MIN_TERM_LENGTH = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.fts_enabled = False

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_column(conn, "sync_jobs", "total_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sync_job_items", "total_count", "INTEGER NOT NULL DEFAULT 0")
            # 1 = user chose "不跟踪": hidden from the list and never synced.
            self._ensure_column(conn, "conversations", "hidden", "INTEGER NOT NULL DEFAULT 0")
            # Why it is hidden: 'user', 'unnamed' (automatic), or 'kept' (user restored it).
            self._ensure_column(conn, "conversations", "hidden_reason", "TEXT")
            auto_hidden = self._apply_unnamed_group_rule(conn, "1=1", ())
            if auto_hidden:
                logger.info("已自动不跟踪 %d 个没有群名的群聊", auto_hidden)
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key='schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 1
            if version < 2:
                self._migrate_to_v2(conn)
            if version < 3:
                self._migrate_to_v3(conn)
            if version < 4:
                self._migrate_to_v4(conn)
            conn.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.fts_enabled = self._ensure_fts(conn)

    @staticmethod
    def _migrate_to_v2(conn: sqlite3.Connection) -> None:
        # v1 stored group messages with the "<username>:\n" prefix in the body
        # and resolved senders through the wrong upstream table. Clearing the
        # cursors makes the next sync re-read every selected conversation; the
        # stable source_key turns that into in-place updates, not duplicates.
        cleared = conn.execute("DELETE FROM sync_cursors").rowcount
        if cleared:
            logger.info("schema v2: 已重置 %d 个同步游标，下次同步将全量重扫以修复群聊正文和发送者", cleared)

    @staticmethod
    def _migrate_to_v3(conn: sqlite3.Connection) -> None:
        # v1/v2 created an unused "summaries" table; topic summaries replace it.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='summaries'"
        ).fetchone()
        if exists and conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0:
            conn.execute("DROP TABLE summaries")

    @staticmethod
    def _migrate_to_v4(conn: sqlite3.Connection) -> None:
        # v1-v3 keyed messages by local_id/real_sender_id, which differ between
        # computers, so syncing a moved archive on another computer inserted
        # every message again. Re-key by server id and merge the copies into
        # the oldest row, keeping topic-summary references.
        from .wechat_adapter import server_message_key

        rows = conn.execute(
            "SELECT id, conversation_id, source_server_id, source_key FROM messages ORDER BY id"
        ).fetchall()
        keep: dict[str, int] = {}
        rekey: list[tuple[str, int]] = []
        merged = 0
        for row in rows:
            key = server_message_key(row["conversation_id"], row["source_server_id"])
            if key is None:
                continue
            if key not in keep:
                keep[key] = row["id"]
                if row["source_key"] != key:
                    rekey.append((key, row["id"]))
                continue
            conn.execute(
                "INSERT OR IGNORE INTO summary_topic_sources(topic_id, message_id) "
                "SELECT topic_id, ? FROM summary_topic_sources WHERE message_id=?",
                (keep[key], row["id"]),
            )
            conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
            merged += 1
        conn.executemany("UPDATE messages SET source_key=? WHERE id=?", rekey)
        if merged:
            logger.info("schema v4: 已合并 %d 条跨电脑重复消息", merged)

    @staticmethod
    def _ensure_fts(conn: sqlite3.Connection) -> bool:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        try:
            conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError as exc:
            logger.warning("SQLite 不支持 FTS5 trigram，关键词搜索回退为 LIKE: %s", exc)
            return False
        if not exists:
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        return True

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_conversations(self, sessions: Iterable[dict[str, Any]]) -> int:
        now = utc_now()
        count = 0
        with self.connect() as conn:
            for item in sessions:
                username = str(item.get("username") or "").strip()
                if not username:
                    continue
                display_name = str(item.get("display_name") or username)
                ctype = "group" if username.endswith("@chatroom") else "direct"
                conn.execute(
                    """
                    INSERT INTO conversations(
                        source_username, display_name, conversation_type,
                        last_source_message_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_username) DO UPDATE SET
                        display_name=excluded.display_name,
                        conversation_type=excluded.conversation_type,
                        last_source_message_at=excluded.last_source_message_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        username,
                        display_name,
                        ctype,
                        item.get("last_time"),
                        now,
                        now,
                    ),
                )
                self._apply_unnamed_group_rule(conn, "source_username=?", (username,))
                count += 1
        return count

    # A group without a name is shown by its raw id ("123456@chatroom").
    _UNNAMED_GROUP = (
        "(source_username LIKE '%@chatroom' AND (TRIM(display_name)='' "
        "OR display_name=source_username OR display_name LIKE '%@chatroom'))"
    )

    @classmethod
    def _apply_unnamed_group_rule(cls, conn: sqlite3.Connection, where: str, params: tuple) -> int:
        """Auto-untrack unnamed groups; show them again once they get a name.

        ``hidden_reason`` keeps the user's own choices intact: 'user' (hidden
        by the user) is never undone here, and 'kept' (restored by the user)
        is never auto-hidden again. Groups selected for sync are left alone.
        """
        hidden = conn.execute(
            f"""
            UPDATE conversations SET hidden=1, hidden_reason='unnamed'
            WHERE {where} AND {cls._UNNAMED_GROUP} AND sync_enabled=0 AND hidden=0
              AND COALESCE(hidden_reason, '')<>'kept'
            """,
            params,
        ).rowcount
        conn.execute(
            f"""
            UPDATE conversations SET hidden=0, hidden_reason=NULL
            WHERE {where} AND NOT {cls._UNNAMED_GROUP} AND hidden_reason='unnamed'
            """,
            params,
        )
        return hidden

    def list_conversations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversations
                ORDER BY sync_enabled DESC, COALESCE(last_source_message_at, 0) DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def configure_conversation(
        self,
        conversation_id: int,
        *,
        enabled: bool,
        history_mode: str,
        history_start_at: int | None,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT history_mode, history_start_at FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if previous is None:
                return None
            # Choosing to sync a conversation also brings it back from "不跟踪".
            conn.execute(
                """
                UPDATE conversations
                SET sync_enabled=?, history_mode=?, history_start_at=?, updated_at=?,
                    hidden=CASE WHEN ? THEN 0 ELSE hidden END,
                    hidden_reason=CASE WHEN ? THEN 'kept' ELSE hidden_reason END
                WHERE id=?
                """,
                (int(enabled), history_mode, history_start_at, utc_now(), int(enabled), int(enabled), conversation_id),
            )
            if (previous["history_mode"], previous["history_start_at"]) != (
                history_mode,
                history_start_at,
            ):
                # An incremental cursor would never reach back to newly
                # requested history, so a range change starts a full pass.
                conn.execute(
                    "DELETE FROM sync_cursors WHERE conversation_id=?", (conversation_id,)
                )
            row = conn.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def selected_conversations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE sync_enabled=1 AND hidden=0 ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_conversations_hidden(self, conversation_ids: list[int], hidden: bool) -> int:
        """Mark conversations as "不跟踪" (hidden, sync off) or show them again.

        Archived messages and summaries are kept either way; restoring does
        not turn syncing back on.
        """
        if not conversation_ids:
            return 0
        marks = ",".join("?" * len(conversation_ids))
        with self.connect() as conn:
            if hidden:
                sql = (
                    "UPDATE conversations SET hidden=1, hidden_reason='user', sync_enabled=0, "
                    f"updated_at=? WHERE id IN ({marks})"
                )
            else:
                # 'kept': the user wants it shown, so automatic rules leave it alone.
                sql = f"UPDATE conversations SET hidden=0, hidden_reason='kept', updated_at=? WHERE id IN ({marks})"
            return conn.execute(sql, [utc_now(), *conversation_ids]).rowcount

    _INACTIVE_WHERE = "hidden=0 AND sync_enabled=0 AND COALESCE(last_source_message_at, 0) < ?"

    def count_inactive_conversations(self, before: int) -> int:
        """Unselected, visible conversations with no message since ``before``."""
        with self.connect() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM conversations WHERE {self._INACTIVE_WHERE}", (before,)
            ).fetchone()[0])

    def hide_inactive_conversations(self, before: int) -> int:
        # Conversations selected for sync are never hidden in bulk.
        with self.connect() as conn:
            return conn.execute(
                f"UPDATE conversations SET hidden=1, hidden_reason='user', updated_at=? WHERE {self._INACTIVE_WHERE}",
                (utc_now(), before),
            ).rowcount

    def create_sync_job(self, job_type: str, conversations: list[dict[str, Any]]) -> int:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_jobs(job_type, status, created_at) VALUES(?, 'queued', ?)",
                (job_type, now),
            )
            job_id = int(cur.lastrowid)
            conn.executemany(
                """
                INSERT INTO sync_job_items(job_id, conversation_id, status)
                VALUES(?, ?, 'queued')
                """,
                [(job_id, int(c["id"])) for c in conversations],
            )
        return job_id

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sync_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def active_job_id(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM sync_jobs WHERE status IN (?, ?) ORDER BY id DESC LIMIT 1",
                ACTIVE_JOB_STATUSES,
            ).fetchone()
        return int(row["id"]) if row else None

    def recover_interrupted_jobs(self) -> int:
        """Close jobs left queued/running by a previous process."""
        now = utc_now()
        with self.connect() as conn:
            count = conn.execute(
                """
                UPDATE sync_jobs SET status='interrupted', finished_at=?,
                    error_message=COALESCE(error_message, '程序在同步过程中退出')
                WHERE status IN (?, ?)
                """,
                (now, *ACTIVE_JOB_STATUSES),
            ).rowcount
            conn.execute(
                """
                UPDATE sync_job_items SET status='interrupted', finished_at=?
                WHERE status IN (?, ?)
                """,
                (now, *ACTIVE_JOB_STATUSES),
            )
        return count

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT j.*,
                       COALESCE((
                           SELECT c.display_name
                           FROM sync_job_items i
                           JOIN conversations c ON c.id=i.conversation_id
                           WHERE i.job_id=j.id AND i.status='running'
                           ORDER BY i.id LIMIT 1
                       ), '') AS current_conversation
                FROM sync_jobs j ORDER BY j.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def query_messages(
        self,
        *,
        conversation_ids: list[int] | None,
        start_at: int | None,
        end_at: int | None,
        query: str | None,
        order: str,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_ids:
            clauses.append("m.conversation_id IN (%s)" % ",".join("?" * len(conversation_ids)))
            params.extend(conversation_ids)
        if start_at is not None:
            clauses.append("m.source_created_at>=?")
            params.append(start_at)
        if end_at is not None:
            clauses.append("m.source_created_at<=?")
            params.append(end_at)
        # Whitespace-separated terms must all match. Terms long enough for the
        # trigram index use FTS5; shorter ones (common in Chinese) use LIKE.
        for term in (query or "").split():
            if self.fts_enabled and len(term) >= FTS_MIN_TERM_LENGTH:
                clauses.append(
                    "m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)"
                )
                params.append('"%s"' % term.replace('"', '""'))
            else:
                clauses.append("m.content LIKE ? ESCAPE '\\'")
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.append(f"%{escaped}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM messages m" + where, params
            ).fetchone()[0]
            direction = "ASC" if order == "asc" else "DESC"
            rows = conn.execute(
                """
                SELECT m.*, c.display_name AS conversation_name
                FROM messages m
                JOIN conversations c ON c.id=m.conversation_id
                """
                + where
                + f" ORDER BY m.source_created_at {direction}, m.sort_seq {direction} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return int(total), [dict(row) for row in rows]

    # -- app settings --------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # -- topic summaries -----------------------------------------------------

    def get_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def summary_messages(
        self, conversation_id: int, start_at: int, end_at: int
    ) -> list[dict[str, Any]]:
        """Archived messages of one conversation in ``[start_at, end_at]``, oldest first."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_created_at, sort_seq, sender_source_id,
                       sender_display_name, is_self, content
                FROM messages
                WHERE conversation_id=? AND source_created_at BETWEEN ? AND ?
                ORDER BY source_created_at, sort_seq, id
                """,
                (conversation_id, start_at, end_at),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_topic_summary(self, **fields: Any) -> int:
        fields.setdefault("status", "queued")
        fields.setdefault("created_at", utc_now())
        columns = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO topic_summaries({columns}) VALUES({marks})", list(fields.values())
            )
        return int(cur.lastrowid)

    _SUMMARY_FIELDS = {
        "status", "message_count", "window_count", "done_windows", "cached_windows",
        "overview", "input_tokens", "output_tokens", "error_message", "finished_at",
    }

    def update_topic_summary(self, summary_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._SUMMARY_FIELDS
        if unknown:
            raise ValueError(f"unknown summary fields: {sorted(unknown)}")
        assignments = ", ".join(f"{column}=?" for column in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE topic_summaries SET {assignments} WHERE id=?",
                [*fields.values(), summary_id],
            )

    def save_summary_topics(self, summary_id: int, topics: list[dict[str, Any]]) -> None:
        """Replace the topics of a summary. Each topic carries ``source_ids``."""
        with self.connect() as conn:
            conn.execute("DELETE FROM summary_topics WHERE summary_id=?", (summary_id,))
            for position, topic in enumerate(topics):
                sources = sorted(set(topic["source_ids"]))
                first_at = last_at = None
                if sources:
                    first_at, last_at = conn.execute(
                        "SELECT MIN(source_created_at), MAX(source_created_at) FROM messages "
                        "WHERE id IN (%s)" % ",".join("?" * len(sources)),
                        sources,
                    ).fetchone()
                cur = conn.execute(
                    """
                    INSERT INTO summary_topics(
                        summary_id, position, title, question, discussion_json,
                        conclusions_json, open_questions_json, participants_json,
                        status, first_message_at, last_message_at, source_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_id, position, topic["title"], topic["question"],
                        _json(topic["discussion"]), _json(topic["conclusions"]),
                        _json(topic["open_questions"]), _json(topic["participants"]),
                        topic["status"], first_at, last_at, len(sources),
                    ),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO summary_topic_sources(topic_id, message_id) VALUES(?, ?)",
                    [(cur.lastrowid, message_id) for message_id in sources],
                )

    _SUMMARY_SELECT = """
        SELECT s.*, c.display_name AS conversation_name,
               (SELECT COUNT(*) FROM summary_topics t WHERE t.summary_id=s.id) AS topic_count,
               (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=s.conversation_id
                  AND m.source_created_at BETWEEN s.range_start AND s.range_end) AS current_message_count
        FROM topic_summaries s JOIN conversations c ON c.id=s.conversation_id
    """

    @staticmethod
    def _with_stale_flag(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        # Messages were archived or repaired in the range after this summary ran.
        item["stale"] = item["status"] == "success" and item["current_message_count"] != item["message_count"]
        return item

    def list_topic_summaries(self, conversation_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Saved summaries, newest generated first. Summaries are kept until the user deletes them."""
        where, params = ("WHERE s.conversation_id=?", [conversation_id]) if conversation_id else ("", [])
        with self.connect() as conn:
            rows = conn.execute(
                f"{self._SUMMARY_SELECT} {where} ORDER BY s.id DESC LIMIT ?", [*params, limit]
            ).fetchall()
        return [self._with_stale_flag(row) for row in rows]

    def get_topic_summary(self, summary_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(f"{self._SUMMARY_SELECT} WHERE s.id=?", (summary_id,)).fetchone()
            if row is None:
                return None
            topics = conn.execute(
                "SELECT * FROM summary_topics WHERE summary_id=? ORDER BY position", (summary_id,)
            ).fetchall()
        summary = self._with_stale_flag(row)
        summary["topics"] = [
            {
                "id": topic["id"],
                "title": topic["title"],
                "question": topic["question"],
                "discussion": json.loads(topic["discussion_json"]),
                "conclusions": json.loads(topic["conclusions_json"]),
                "open_questions": json.loads(topic["open_questions_json"]),
                "participants": json.loads(topic["participants_json"]),
                "status": topic["status"],
                "first_message_at": topic["first_message_at"],
                "last_message_at": topic["last_message_at"],
                "source_count": topic["source_count"],
            }
            for topic in topics
        ]
        return summary

    def topic_source_messages(self, topic_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.source_created_at, m.sender_source_id, m.sender_display_name,
                       m.is_self, m.content
                FROM summary_topic_sources s JOIN messages m ON m.id=s.message_id
                WHERE s.topic_id=? ORDER BY m.source_created_at, m.sort_seq, m.id
                """,
                (topic_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_topic_summary(self, summary_id: int) -> bool:
        with self.connect() as conn:
            return conn.execute("DELETE FROM topic_summaries WHERE id=?", (summary_id,)).rowcount > 0

    def recover_interrupted_summaries(self) -> int:
        with self.connect() as conn:
            return conn.execute(
                """
                UPDATE topic_summaries SET status='interrupted', finished_at=?,
                    error_message=COALESCE(error_message, '程序在总结过程中退出')
                WHERE status IN ('queued', 'running')
                """,
                (utc_now(),),
            ).rowcount

    def cached_window(self, input_hash: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_window_cache WHERE input_hash=?", (input_hash,)
            ).fetchone()
        return dict(row) if row else None

    def count_cached_windows(self, hashes: list[str]) -> int:
        if not hashes:
            return 0
        with self.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM ai_window_cache WHERE input_hash IN (%s)"
                    % ",".join("?" * len(hashes)),
                    hashes,
                ).fetchone()[0]
            )

    def store_cached_window(
        self, input_hash: str, *, prompt_version: str, model: str, result: dict[str, Any],
        input_tokens: int, output_tokens: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ai_window_cache(
                    input_hash, prompt_version, model, result_json,
                    input_tokens, output_tokens, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (input_hash, prompt_version, model, _json(result), input_tokens, output_tokens, utc_now()),
            )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

