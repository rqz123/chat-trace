from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Callable

from .config import Settings
from .storage import Storage, utc_now
from .wechat_adapter import MessageWindow, WeChatReader, WeChatStatus, message_source_key


TEXT_TYPES = {"文本", "text"}
HISTORY_WINDOW_DAYS = {"recent_30d": 30, "recent_90d": 90}
BATCH_SIZE = 500

logger = logging.getLogger("chattrace.sync")


class SyncCancelled(Exception):
    pass


def _timestamp(value: Any) -> int:
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if stamp > 10_000_000_000:
        stamp //= 1000
    return stamp


def history_cutoff(conversation: dict[str, Any], now: float) -> int:
    """Earliest source time a full pass should read for this conversation."""
    mode = conversation.get("history_mode") or "all"
    if mode == "since":
        return int(conversation.get("history_start_at") or 0)
    if mode in HISTORY_WINDOW_DAYS:
        return int(now) - HISTORY_WINDOW_DAYS[mode] * 24 * 60 * 60
    return 0


class SyncService:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        reader_factory: Callable[[], WeChatReader] | None = None,
        status_check: Callable[[], WeChatStatus] | None = None,
    ):
        self.storage = storage
        self.settings = settings
        self.reader_factory = reader_factory or (lambda: WeChatReader(settings))
        self.status_check = status_check or (lambda: WeChatReader(settings).status())
        self._lock = threading.Lock()
        self._cancel_events: dict[int, threading.Event] = {}

    def start(
        self, *, wait: bool = False, on_created: Callable[[int], None] | None = None
    ) -> int:
        """Create a sync job and run it in a worker thread (or inline with ``wait``).

        ``on_created`` receives the job id before the job runs, so a caller
        blocked in ``wait`` mode can still be cancelled by id.
        Raises ValueError when nothing is selected, a job is already running,
        or WeChat is not in a state that allows reading.
        """
        conversations = self.storage.selected_conversations()
        if not conversations:
            raise ValueError("尚未选择任何需要同步的会话")
        if not self._lock.acquire(blocking=False):
            raise ValueError("已有同步任务正在运行")
        try:
            status = self.status_check()
            if not status.can_sync:
                raise ValueError(status.message)
            job_id = self.storage.create_sync_job("manual", conversations)
        except BaseException:
            self._lock.release()
            raise
        cancel_event = threading.Event()
        self._cancel_events[job_id] = cancel_event
        if on_created is not None:
            on_created(job_id)
        if wait:
            self._run(job_id, conversations, cancel_event)
        else:
            threading.Thread(
                target=self._run,
                args=(job_id, conversations, cancel_event),
                daemon=True,
                name=f"chattrace-sync-{job_id}",
            ).start()
        return job_id

    def cancel(self, job_id: int) -> bool:
        event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def _run(
        self, job_id: int, conversations: list[dict[str, Any]], cancel_event: threading.Event
    ) -> None:
        reader = None
        logger.info("同步任务 #%d 开始，共 %d 个会话", job_id, len(conversations))
        try:
            with self.storage.connect() as conn:
                conn.execute(
                    "UPDATE sync_jobs SET status='running', started_at=? WHERE id=?",
                    (utc_now(), job_id),
                )
            reader = self.reader_factory()
            errors = 0
            cancelled = False
            for conversation in conversations:
                if cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    self._sync_conversation(job_id, conversation, reader, cancel_event)
                except SyncCancelled:
                    cancelled = True
                    break
                except Exception as exc:
                    errors += 1
                    # Log the conversation's local id only: never names or content.
                    logger.warning(
                        "同步任务 #%d 会话 #%s 失败: %s: %s",
                        job_id, conversation["id"], type(exc).__name__, str(exc)[:200],
                    )
                    with self.storage.connect() as conn:
                        conn.execute(
                            """
                            UPDATE sync_job_items
                            SET status='failed', error_message=?, finished_at=?
                            WHERE job_id=? AND conversation_id=?
                            """,
                            (str(exc)[:1000], utc_now(), job_id, conversation["id"]),
                        )
            if cancelled:
                with self.storage.connect() as conn:
                    conn.execute(
                        """
                        UPDATE sync_job_items SET status='cancelled', finished_at=?
                        WHERE job_id=? AND status IN ('queued', 'running')
                        """,
                        (utc_now(), job_id),
                    )
            self._recount_job(job_id)
            if cancelled:
                status = "cancelled"
            elif errors and errors == len(conversations):
                status = "failed"
            elif errors:
                status = "partial"
            else:
                status = "success"
            self._finish_job(job_id, status, None)
            logger.info("同步任务 #%d 结束: %s", job_id, status)
        except Exception as exc:
            logger.exception("同步任务 #%d 异常终止", job_id)
            self._finish_job(job_id, "failed", str(exc)[:1000])
        finally:
            if reader is not None and hasattr(reader, "close"):
                try:
                    reader.close()
                except Exception:
                    logger.exception("清理解密副本失败")
            self._cancel_events.pop(job_id, None)
            self._lock.release()

    def _sync_conversation(
        self,
        job_id: int,
        conversation: dict[str, Any],
        reader: WeChatReader,
        cancel_event: threading.Event,
    ) -> None:
        cid = int(conversation["id"])
        with self.storage.connect() as conn:
            conn.execute(
                """
                UPDATE sync_job_items SET status='running', started_at=?
                WHERE job_id=? AND conversation_id=?
                """,
                (utc_now(), job_id, cid),
            )
            cursor = conn.execute(
                "SELECT * FROM sync_cursors WHERE conversation_id=?", (cid,)
            ).fetchone()

        last_sort_seq = int(cursor["last_sort_seq"]) if cursor else 0
        last_message_at = int(cursor["last_message_at"]) if cursor else 0
        incremental = cursor is not None
        cutoff = history_cutoff(conversation, time.time())
        if incremental:
            window = MessageWindow(
                history_cutoff=cutoff,
                after_sort_seq=last_sort_seq,
                lookback_from=max(0, last_message_at - self.settings.incremental_lookback_seconds),
            )
        else:
            window = MessageWindow(history_cutoff=cutoff)

        nicknames = reader.nicknames() if hasattr(reader, "nicknames") else {}
        scanned = inserted = updated = skipped = 0
        max_sort_seq = last_sort_seq
        max_message_at = last_message_at

        with reader.open_conversation(str(conversation["source_username"])) as source:
            total_count = source.count(window) if source is not None else 0
            with self.storage.connect() as conn:
                conn.execute(
                    """
                    UPDATE sync_job_items SET total_count=?
                    WHERE job_id=? AND conversation_id=?
                    """,
                    (total_count, job_id, cid),
                )
            self._recount_job(job_id)

            batches = source.batches(window, BATCH_SIZE) if source is not None else []
            for batch in batches:
                if cancel_event.is_set():
                    raise SyncCancelled()
                prepared: list[dict[str, Any]] = []
                for message in batch:
                    scanned += 1
                    created_at = _timestamp(message.get("create_time"))
                    sort_seq = int(message.get("sort_seq") or 0)
                    # Advance over every scanned row, not only text, so a chat
                    # with recent media does not rescan from 0 each time.
                    max_sort_seq = max(max_sort_seq, sort_seq)
                    max_message_at = max(max_message_at, created_at)
                    if str(message.get("type") or "") not in TEXT_TYPES:
                        skipped += 1
                        continue
                    content = str(message.get("content") or "").strip()
                    if not content:
                        skipped += 1
                        continue
                    sender_username = str(message.get("sender_username") or "")
                    sender_id = message.get("sender_id")
                    prepared.append(
                        {
                            "source_key": message_source_key(cid, message),
                            "source_local_id": message.get("local_id"),
                            "source_server_id": message.get("server_id"),
                            "sort_seq": sort_seq,
                            "sender_source_id": sender_username or (str(sender_id) if sender_id else None),
                            "sender_display_name": nicknames.get(sender_username) or sender_username or None,
                            "is_self": int(bool(message.get("is_self"))),
                            "content": content,
                            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                            "source_created_at": created_at,
                        }
                    )

                prepared.sort(key=lambda item: (item["source_created_at"], item["sort_seq"]))
                counts = self._write_batch(cid, prepared)
                inserted += counts[0]
                updated += counts[1]
                skipped += counts[2]
                with self.storage.connect() as conn:
                    conn.execute(
                        """
                        UPDATE sync_job_items
                        SET scanned_count=?, inserted_count=?, updated_count=?, skipped_count=?
                        WHERE job_id=? AND conversation_id=?
                        """,
                        (scanned, inserted, updated, skipped, job_id, cid),
                    )
                self._recount_job(job_id)

        with self.storage.connect() as conn:
            now = utc_now()
            conn.execute(
                """
                INSERT INTO sync_cursors(
                    conversation_id, last_sort_seq, last_message_at,
                    last_full_rescan_at, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    last_sort_seq=excluded.last_sort_seq,
                    last_message_at=excluded.last_message_at,
                    last_full_rescan_at=CASE
                        WHEN ?=0 THEN excluded.last_full_rescan_at
                        ELSE sync_cursors.last_full_rescan_at END,
                    updated_at=excluded.updated_at
                """,
                (cid, max_sort_seq, max_message_at, now, now, int(incremental)),
            )
            conn.execute(
                """
                UPDATE conversations
                SET last_successful_sync_at=?,
                    archived_message_count=(
                        SELECT COUNT(*) FROM messages WHERE conversation_id=?
                    ), updated_at=?
                WHERE id=?
                """,
                (now, cid, now, cid),
            )
            conn.execute(
                """
                UPDATE sync_job_items
                SET status='success', scanned_count=?, inserted_count=?,
                    updated_count=?, skipped_count=?, finished_at=?
                WHERE job_id=? AND conversation_id=?
                """,
                (scanned, inserted, updated, skipped, now, job_id, cid),
            )

    def _write_batch(self, cid: int, prepared: list[dict[str, Any]]) -> tuple[int, int, int]:
        """Insert or update one batch in a single transaction.

        Returns ``(inserted, updated, unchanged)``.
        """
        inserted = updated = unchanged = 0
        now = utc_now()
        tracked = ("content_hash", "sender_source_id", "sender_display_name", "is_self", "source_server_id")
        with self.storage.connect() as conn:
            for item in prepared:
                existing = conn.execute(
                    f"SELECT id, {', '.join(tracked)} FROM messages WHERE source_key=?",
                    (item["source_key"],),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO messages(
                            conversation_id, source_key, source_local_id, source_server_id,
                            sort_seq, sender_source_id, sender_display_name, is_self,
                            message_type, content, content_hash,
                            source_created_at, archived_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'text', ?, ?, ?, ?, ?)
                        """,
                        (
                            cid,
                            item["source_key"],
                            item["source_local_id"],
                            item["source_server_id"],
                            item["sort_seq"],
                            item["sender_source_id"],
                            item["sender_display_name"],
                            item["is_self"],
                            item["content"],
                            item["content_hash"],
                            item["source_created_at"],
                            now,
                            now,
                        ),
                    )
                    inserted += 1
                elif any(existing[column] != item[column] for column in tracked):
                    conn.execute(
                        """
                        UPDATE messages
                        SET content=?, content_hash=?, sender_source_id=?,
                            sender_display_name=?, is_self=?, source_server_id=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            item["content"],
                            item["content_hash"],
                            item["sender_source_id"],
                            item["sender_display_name"],
                            item["is_self"],
                            item["source_server_id"],
                            now,
                            existing["id"],
                        ),
                    )
                    updated += 1
                else:
                    unchanged += 1
        return inserted, updated, unchanged

    def _recount_job(self, job_id: int) -> None:
        with self.storage.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(scanned_count), 0),
                       COALESCE(SUM(total_count), 0),
                       COALESCE(SUM(inserted_count), 0),
                       COALESCE(SUM(updated_count), 0),
                       COALESCE(SUM(skipped_count), 0),
                       COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0)
                FROM sync_job_items WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE sync_jobs SET scanned_count=?, inserted_count=?, updated_count=?,
                    skipped_count=?, error_count=?, total_count=? WHERE id=?
                """,
                (row[0], row[2], row[3], row[4], row[5], row[1], job_id),
            )

    def _finish_job(self, job_id: int, status: str, error: str | None) -> None:
        with self.storage.connect() as conn:
            conn.execute(
                """
                UPDATE sync_jobs SET status=?, error_message=?, finished_at=? WHERE id=?
                """,
                (status, error, utc_now(), job_id),
            )
