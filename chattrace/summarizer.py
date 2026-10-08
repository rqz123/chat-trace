"""Topic summaries of one conversation over a time range.

Messages in the range are split into windows (by time gap and size). Each
window is sent to the model once and the structured result is cached by input
hash. When a range has several windows, a merge call combines topics that span
windows. Every topic keeps the ids of the archived messages that support it;
ids the model returns that were not in its input are dropped.

Only the selected conversation and range are sent, with ``store=False``.
Message bodies are never logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, TypeVar

from pydantic import BaseModel

from .config import Settings
from .storage import Storage, utc_now


logger = logging.getLogger("chattrace.summarizer")

PROMPT_DIR = Path(__file__).with_name("prompts")
EXTRACTION_PROMPT_VERSION = "wechat-topic-extraction-v3"
MERGE_PROMPT_VERSION = "wechat-topic-merge-v3"

# Same tiers as zoom-chat-kb-hub so both tools behave alike with one key.
MODEL_TIERS = {"low": "gpt-5-mini", "mid": "gpt-5.6-luna", "high": "gpt-5.6-terra"}
DEFAULT_MODEL_TIER = "mid"
FALLBACK_CONFIG = Path(
    os.environ.get("CHATTRACE_OPENAI_FALLBACK_CONFIG", r"C:\Works\access-redmine\openai_config.json")
)

WINDOW_GAP_SECONDS = 6 * 60 * 60
WINDOW_MAX_MESSAGES = 150
WINDOW_MAX_CHARS = 20_000
MESSAGE_MAX_CHARS = 2_000
MAX_PARALLEL_WINDOWS = 4
MAX_RANGE_MESSAGES = 6_000

TopicStatus = Literal["resolved", "ongoing", "inconclusive"]


class TopicDraft(BaseModel):
    title: str
    question: str
    discussion: list[str]
    conclusions: list[str]
    open_questions: list[str]
    status: TopicStatus
    participants: list[str]
    source_message_ids: list[int]


class WindowResult(BaseModel):
    overview: str
    topics: list[TopicDraft]


class MergedTopic(BaseModel):
    member_ids: list[int]
    title: str
    question: str
    discussion: list[str]
    conclusions: list[str]
    open_questions: list[str]
    status: TopicStatus
    participants: list[str]


class MergeResult(BaseModel):
    overview: str
    topics: list[MergedTopic]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AISettings:
    api_key: str
    model: str
    base_url: str
    source: str
    model_tier: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def public(self) -> dict[str, Any]:
        """Safe to return to the UI: never includes the key."""
        return {
            "configured": self.configured,
            "model": self.model,
            "model_tier": self.model_tier,
            "source": self.source,
            "tiers": MODEL_TIERS,
        }


def load_ai_settings(settings: Settings, model_tier: str | None) -> AISettings:
    """Same priority as zoom-chat-kb-hub: environment, OPENAI_CONFIG_FILE,
    ``<data_dir>/openai_config.json``, then the shared fallback file."""
    candidates: list[Path] = []
    if os.environ.get("OPENAI_CONFIG_FILE"):
        candidates.append(Path(os.environ["OPENAI_CONFIG_FILE"]))
    candidates.extend([settings.data_dir / "openai_config.json", FALLBACK_CONFIG])
    payload: dict[str, Any] = {}
    source = "未找到配置"
    for path in candidates:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(candidate, dict):
            payload, source = candidate, str(path)
            break
    tier = model_tier if model_tier in MODEL_TIERS else DEFAULT_MODEL_TIER
    env_key = os.environ.get("OPENAI_API_KEY")
    return AISettings(
        api_key=env_key or str(payload.get("api_key") or ""),
        model=os.environ.get("OPENAI_MODEL") or MODEL_TIERS[tier],
        base_url=os.environ.get("OPENAI_BASE_URL", str(payload.get("base_url") or "")),
        source="环境变量 OPENAI_API_KEY" if env_key else source,
        model_tier=tier,
    )


# ---------------------------------------------------------------------------
# Model access
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


class LLM(Protocol):
    def parse(self, instructions: str, payload: dict[str, Any], schema: type[T]) -> tuple[T, dict[str, int]]: ...


class OpenAILLM:
    def __init__(self, ai: AISettings):
        from openai import OpenAI

        kwargs: dict[str, Any] = {"api_key": ai.api_key, "timeout": 180.0, "max_retries": 2}
        if ai.base_url:
            kwargs["base_url"] = ai.base_url
        self.client = OpenAI(**kwargs)
        self.model = ai.model

    def parse(self, instructions: str, payload: dict[str, Any], schema: type[T]) -> tuple[T, dict[str, int]]:
        response = self.client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=json.dumps(payload, ensure_ascii=False),
            text_format=schema,
            store=False,
        )
        if response.output_parsed is None:
            raise RuntimeError("模型没有返回可解析的结果")
        usage = getattr(response, "usage", None)
        return response.output_parsed, {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def sender_label(message: dict[str, Any]) -> str:
    if message.get("is_self"):
        return "我"
    return str(message.get("sender_display_name") or message.get("sender_source_id") or "未知")


def build_windows(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split oldest-first messages on long silences and size limits."""
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for message in messages:
        length = min(len(message["content"]), MESSAGE_MAX_CHARS)
        if current and (
            message["source_created_at"] - current[-1]["source_created_at"] > WINDOW_GAP_SECONDS
            or len(current) >= WINDOW_MAX_MESSAGES
            or chars + length > WINDOW_MAX_CHARS
        ):
            windows.append(current)
            current, chars = [], 0
        current.append(message)
        chars += length
    if current:
        windows.append(current)
    return windows


def window_payload(conversation: dict[str, Any], window: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "conversation": conversation["display_name"],
        "conversation_type": "群聊" if conversation["conversation_type"] == "group" else "单聊",
        "messages": [
            {
                "id": message["id"],
                "time": datetime.fromtimestamp(message["source_created_at"]).strftime("%Y-%m-%d %H:%M"),
                "sender": sender_label(message),
                "text": message["content"][:MESSAGE_MAX_CHARS],
            }
            for message in window
        ],
    }


def window_hash(model: str, payload: dict[str, Any]) -> str:
    material = {"prompt_version": EXTRACTION_PROMPT_VERSION, "model": model, "payload": payload}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SummaryCancelled(Exception):
    pass


class TopicSummarizer:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        llm_factory: Callable[[AISettings], LLM] | None = None,
    ):
        self.storage = storage
        self.settings = settings
        self.llm_factory = llm_factory or OpenAILLM
        self._cancel_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    # -- settings ------------------------------------------------------------

    def ai_settings(self) -> AISettings:
        return load_ai_settings(self.settings, self.storage.get_meta("ai_model_tier"))

    def set_model_tier(self, tier: str) -> AISettings:
        if tier not in MODEL_TIERS:
            raise ValueError("未知的模型档位")
        self.storage.set_meta("ai_model_tier", tier)
        return self.ai_settings()

    # -- preview -------------------------------------------------------------

    def _load(self, conversation_id: int, start_at: int, end_at: int):
        if end_at < start_at:
            raise ValueError("结束时间不能早于开始时间")
        conversation = self.storage.get_conversation(conversation_id)
        if conversation is None:
            raise LookupError("会话不存在")
        messages = self.storage.summary_messages(conversation_id, start_at, end_at)
        return conversation, messages

    def preview(self, conversation_id: int, start_at: int, end_at: int) -> dict[str, Any]:
        conversation, messages = self._load(conversation_id, start_at, end_at)
        ai = self.ai_settings()
        windows = build_windows(messages)
        hashes = [window_hash(ai.model, window_payload(conversation, w)) for w in windows]
        cached = self.storage.count_cached_windows(hashes)
        characters = sum(len(m["content"]) for m in messages)
        return {
            "message_count": len(messages),
            "character_count": characters,
            "participants": len({sender_label(m) for m in messages}),
            "actual_start": messages[0]["source_created_at"] if messages else None,
            "actual_end": messages[-1]["source_created_at"] if messages else None,
            "window_count": len(windows),
            "cached_windows": cached,
            "too_many": len(messages) > MAX_RANGE_MESSAGES,
            "max_messages": MAX_RANGE_MESSAGES,
            "ai": ai.public(),
        }

    # -- runs ----------------------------------------------------------------

    def start(
        self, conversation_id: int, start_at: int, end_at: int, range_label: str = "", *, wait: bool = False
    ) -> int:
        ai = self.ai_settings()
        if not ai.configured:
            raise ValueError("未配置 OpenAI API key（见 README「话题总结」一节）")
        conversation, messages = self._load(conversation_id, start_at, end_at)
        if not messages:
            raise ValueError("所选时间段内没有已归档的文字消息")
        if len(messages) > MAX_RANGE_MESSAGES:
            raise ValueError(f"消息超过 {MAX_RANGE_MESSAGES} 条，请缩短时间段")
        summary_id = self.storage.create_topic_summary(
            conversation_id=conversation_id,
            range_start=start_at,
            range_end=end_at,
            range_label=range_label[:40],
            model=ai.model,
            prompt_version=f"{EXTRACTION_PROMPT_VERSION}+{MERGE_PROMPT_VERSION}",
            message_count=len(messages),
        )
        cancel = threading.Event()
        with self._lock:
            self._cancel_events[summary_id] = cancel
        args = (summary_id, ai, conversation, messages, cancel)
        if wait:
            self._run(*args)
        else:
            threading.Thread(target=self._run, args=args, daemon=True, name=f"chattrace-summary-{summary_id}").start()
        return summary_id

    def cancel(self, summary_id: int) -> bool:
        with self._lock:
            event = self._cancel_events.get(summary_id)
        if event is None:
            return False
        event.set()
        return True

    def _run(self, summary_id, ai, conversation, messages, cancel) -> None:
        logger.info("话题总结 #%d 开始：%d 条消息，模型 %s", summary_id, len(messages), ai.model)
        tokens = {"input_tokens": 0, "output_tokens": 0}
        try:
            llm = self.llm_factory(ai)
            windows = build_windows(messages)
            self.storage.update_topic_summary(summary_id, status="running", window_count=len(windows))
            results = self._extract_windows(summary_id, ai, llm, conversation, windows, cancel, tokens)
            if cancel.is_set():
                raise SummaryCancelled()
            overview, topics = self._combine(llm, results, tokens)
            self.storage.save_summary_topics(summary_id, topics)
            self.storage.update_topic_summary(
                summary_id, status="success", overview=overview, finished_at=utc_now(), **tokens
            )
            logger.info("话题总结 #%d 完成：%d 个话题，tokens %s", summary_id, len(topics), tokens)
        except SummaryCancelled:
            self.storage.update_topic_summary(summary_id, status="cancelled", finished_at=utc_now(), **tokens)
        except Exception as exc:
            logger.warning("话题总结 #%d 失败: %s", summary_id, type(exc).__name__)
            self.storage.update_topic_summary(
                summary_id, status="failed", error_message=str(exc)[:500], finished_at=utc_now(), **tokens
            )
        finally:
            with self._lock:
                self._cancel_events.pop(summary_id, None)

    def _extract_windows(self, summary_id, ai, llm, conversation, windows, cancel, tokens):
        """Return ``[(window, WindowResult)]`` in window order."""
        progress = {"done": 0, "cached": 0}
        progress_lock = threading.Lock()
        prompt = (PROMPT_DIR / "topic_extraction.md").read_text(encoding="utf-8")

        def one(window):
            if cancel.is_set():
                return window, None
            payload = window_payload(conversation, window)
            key = window_hash(ai.model, payload)
            cached = self.storage.cached_window(key)
            if cached:
                result = WindowResult.model_validate_json(cached["result_json"])
                usage = None
            else:
                result, usage = llm.parse(prompt, payload, WindowResult)
                self.storage.store_cached_window(
                    key, prompt_version=EXTRACTION_PROMPT_VERSION, model=ai.model,
                    result=result.model_dump(), **usage,
                )
            with progress_lock:
                progress["done"] += 1
                if usage is None:
                    progress["cached"] += 1
                else:
                    tokens["input_tokens"] += usage["input_tokens"]
                    tokens["output_tokens"] += usage["output_tokens"]
                self.storage.update_topic_summary(
                    summary_id, done_windows=progress["done"], cached_windows=progress["cached"], **tokens
                )
            return window, result

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WINDOWS) as pool:
            return [item for item in pool.map(one, windows) if item[1] is not None]

    @staticmethod
    def _drafts(results) -> list[dict[str, Any]]:
        """Flatten window topics, keeping only source ids present in each window."""
        drafts = []
        for window, result in results:
            valid_ids = {message["id"] for message in window}
            for topic in result.topics:
                sources = [i for i in dict.fromkeys(topic.source_message_ids) if i in valid_ids]
                if not sources:
                    continue  # unsupported by its own input: drop rather than show
                item = topic.model_dump(exclude={"source_message_ids"})
                item["source_ids"] = sources
                item["first_at"] = min(m["source_created_at"] for m in window if m["id"] in set(sources))
                drafts.append(item)
        return drafts

    def _combine(self, llm, results, tokens) -> tuple[str, list[dict[str, Any]]]:
        drafts = self._drafts(results)
        if len(results) <= 1 or len(drafts) <= 1:
            overview = results[0][1].overview if len(results) == 1 else ""
            return overview, sorted(drafts, key=lambda d: d["first_at"])

        payload = {
            "topics": [
                {
                    "id": index,
                    "first_message_time": datetime.fromtimestamp(draft["first_at"]).strftime("%Y-%m-%d %H:%M"),
                    **{key: draft[key] for key in (
                        "title", "question", "discussion", "conclusions", "open_questions", "status", "participants"
                    )},
                }
                for index, draft in enumerate(drafts)
            ]
        }
        prompt = (PROMPT_DIR / "topic_merge.md").read_text(encoding="utf-8")
        merged, usage = llm.parse(prompt, payload, MergeResult)
        tokens["input_tokens"] += usage["input_tokens"]
        tokens["output_tokens"] += usage["output_tokens"]

        used: set[int] = set()
        final: list[dict[str, Any]] = []
        for topic in merged.topics:
            members = [i for i in dict.fromkeys(topic.member_ids) if 0 <= i < len(drafts) and i not in used]
            if not members:
                continue
            used.update(members)
            item = topic.model_dump(exclude={"member_ids"})
            item["source_ids"] = [s for i in members for s in drafts[i]["source_ids"]]
            item["first_at"] = min(drafts[i]["first_at"] for i in members)
            final.append(item)
        # Never lose a topic the merge step forgot to place.
        final.extend(draft for index, draft in enumerate(drafts) if index not in used)
        return merged.overview, sorted(final, key=lambda d: d["first_at"])
