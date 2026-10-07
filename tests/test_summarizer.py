import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chattrace import summarizer as summarizer_module
from chattrace.app import create_app
from chattrace.storage import Storage
from chattrace.summarizer import (
    MergeResult,
    TopicSummarizer,
    WindowResult,
    build_windows,
    load_ai_settings,
)

from fakes import make_settings

HOUR = 3600
BASE = 1_760_000_000


class FakeLLM:
    """Scripted model: one topic per window citing its messages (plus an
    invented id), and a merge that combines the first two topics."""

    def __init__(self, merge_member_ids=None, fail=False):
        self.calls = []
        self.merge_member_ids = merge_member_ids
        self.fail = fail

    def parse(self, instructions, payload, schema):
        self.calls.append((schema.__name__, payload))
        if self.fail:
            raise RuntimeError("API down")
        usage = {"input_tokens": 100, "output_tokens": 10}
        if schema is WindowResult:
            ids = [m["id"] for m in payload["messages"]]
            first = payload["messages"][0]["text"]
            return WindowResult(overview=f"概览:{first}", topics=[{
                "title": f"话题:{first}", "question": "q", "discussion": ["d"], "conclusions": [],
                "open_questions": ["?"], "status": "ongoing", "participants": ["张三"],
                "source_message_ids": ids + [999_999],
            }]), usage
        members = self.merge_member_ids if self.merge_member_ids is not None else [[0, 1, 77], [1]]
        return MergeResult(overview="整体概览", topics=[
            {"member_ids": group, "title": f"合并{group}", "question": "q", "discussion": ["d"],
             "conclusions": ["定了"], "open_questions": [], "status": "resolved", "participants": ["张三"]}
            for group in members
        ]), usage


def seed(tmp_path: Path, times: list[int]):
    settings = make_settings(tmp_path)
    storage = Storage(settings.db_path)
    storage.initialize()
    storage.upsert_conversations([{"username": "g@chatroom", "display_name": "项目群"}])
    cid = storage.list_conversations()[0]["id"]
    with storage.connect() as conn:
        for index, at in enumerate(times):
            conn.execute(
                "INSERT INTO messages(conversation_id, source_key, sort_seq, sender_source_id,"
                " sender_display_name, is_self, content, content_hash, source_created_at, archived_at, updated_at)"
                " VALUES(?, ?, ?, 'wxid_a', '张三', ?, ?, 'h', ?, 'n', 'n')",
                (cid, f"k{index}", index, int(index % 3 == 0), f"消息{index}", at),
            )
    return settings, storage, cid


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")


def make(tmp_path, times, llm):
    settings, storage, cid = seed(tmp_path, times)
    return TopicSummarizer(storage, settings, llm_factory=lambda _ai: llm), storage, cid


def test_build_windows_splits_on_gap_and_size(monkeypatch):
    messages = [{"id": i, "source_created_at": BASE + i * 60, "content": "x"} for i in range(5)]
    messages.append({"id": 9, "source_created_at": BASE + 7 * HOUR, "content": "late"})
    assert [len(w) for w in build_windows(messages)] == [5, 1]
    monkeypatch.setattr(summarizer_module, "WINDOW_MAX_MESSAGES", 2)
    assert [len(w) for w in build_windows(messages)] == [2, 2, 1, 1]


def test_single_window_drops_invented_ids_and_skips_merge(tmp_path):
    llm = FakeLLM()
    service, storage, cid = make(tmp_path, [BASE, BASE + 60, BASE + 120], llm)
    summary_id = service.start(cid, BASE, BASE + HOUR, "最近 1 天", wait=True)
    summary = storage.get_topic_summary(summary_id)
    assert summary["status"] == "success", summary["error_message"]
    assert [name for name, _ in llm.calls] == ["WindowResult"]
    assert summary["overview"] == "概览:消息0"
    (topic,) = summary["topics"]
    assert topic["source_count"] == 3  # 999999 was not in the input
    assert topic["first_message_at"] == BASE and topic["last_message_at"] == BASE + 120
    senders = [m["sender"] for m in llm.calls[0][1]["messages"]]
    assert senders == ["我", "张三", "张三"]
    assert {m["content"] for m in storage.topic_source_messages(topic["id"])} == {"消息0", "消息1", "消息2"}
    assert (summary["input_tokens"], summary["output_tokens"]) == (100, 10)


def test_multi_window_merge_unions_sources_and_keeps_unplaced(tmp_path):
    times = [BASE, BASE + 60, BASE + 8 * HOUR, BASE + 16 * HOUR]  # three windows
    llm = FakeLLM()
    service, storage, cid = make(tmp_path, times, llm)
    summary = storage.get_topic_summary(service.start(cid, BASE, BASE + 20 * HOUR, wait=True))
    assert [name for name, _ in llm.calls].count("MergeResult") == 1
    titles = [t["title"] for t in summary["topics"]]
    # [0,1,77] -> merged (77 invalid ignored); [1] already used -> dropped; topic 2 unplaced -> kept.
    assert titles == ["合并[0, 1, 77]", "话题:消息3"]
    assert [t["source_count"] for t in summary["topics"]] == [3, 1]
    assert summary["topics"][0]["status"] == "resolved"
    assert summary["overview"] == "整体概览"
    assert summary["window_count"] == 3 and summary["done_windows"] == 3


def test_rerun_uses_window_cache(tmp_path):
    llm = FakeLLM()
    service, storage, cid = make(tmp_path, [BASE, BASE + 8 * HOUR], llm)
    service.start(cid, BASE, BASE + 10 * HOUR, wait=True)
    assert service.preview(cid, BASE, BASE + 10 * HOUR)["cached_windows"] == 2
    llm.calls.clear()
    second = storage.get_topic_summary(service.start(cid, BASE, BASE + 10 * HOUR, wait=True))
    assert [name for name, _ in llm.calls] == ["MergeResult"]
    assert second["cached_windows"] == 2


def test_validation_errors(tmp_path, monkeypatch):
    service, storage, cid = make(tmp_path, [BASE], FakeLLM())
    with pytest.raises(ValueError, match="没有已归档"):
        service.start(cid, BASE + 10, BASE + 20, wait=True)
    with pytest.raises(ValueError, match="结束时间"):
        service.preview(cid, BASE, BASE - 1)
    with pytest.raises(LookupError):
        service.preview(999, BASE, BASE + 1)
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(summarizer_module, "FALLBACK_CONFIG", Path(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="API key"):
        service.start(cid, BASE, BASE + 1, wait=True)
    assert storage.list_topic_summaries() == []


def test_failure_is_recorded(tmp_path):
    service, storage, cid = make(tmp_path, [BASE], FakeLLM(fail=True))
    summary = storage.get_topic_summary(service.start(cid, BASE, BASE + 1, wait=True))
    assert summary["status"] == "failed" and "API down" in summary["error_message"]


def test_cancel_before_windows_run(tmp_path):
    llm = FakeLLM()
    service, storage, cid = make(tmp_path, [BASE, BASE + 8 * HOUR], llm)
    original = service.llm_factory

    def cancelling_factory(ai):
        service.cancel(storage.list_topic_summaries()[0]["id"])
        return original(ai)

    service.llm_factory = cancelling_factory
    summary_id = service.start(cid, BASE, BASE + 10 * HOUR, wait=True)
    assert storage.get_topic_summary(summary_id)["status"] == "cancelled"
    assert llm.calls == []


def test_stale_flag_after_new_messages(tmp_path):
    service, storage, cid = make(tmp_path, [BASE], FakeLLM())
    summary_id = service.start(cid, BASE, BASE + HOUR, wait=True)
    assert storage.get_topic_summary(summary_id)["stale"] is False
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO messages(conversation_id, source_key, sort_seq, content, content_hash,"
            " source_created_at, archived_at, updated_at) VALUES(?, 'late', 9, '新消息', 'h', ?, 'n', 'n')",
            (cid, BASE + 30),
        )
    assert storage.get_topic_summary(summary_id)["stale"] is True


def test_ai_settings_priority(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    fallback = tmp_path / "fallback.json"
    fallback.write_text(json.dumps({"api_key": "sk-fallback", "model": "ignored"}), encoding="utf-8")
    monkeypatch.setattr(summarizer_module, "FALLBACK_CONFIG", fallback)
    monkeypatch.delenv("OPENAI_API_KEY")
    ai = load_ai_settings(settings, "low")
    assert (ai.api_key, ai.model, ai.source) == ("sk-fallback", "gpt-5-mini", str(fallback))
    (tmp_path / "openai_config.json").write_text(json.dumps({"api_key": "sk-local"}), encoding="utf-8")
    assert load_ai_settings(settings, None).api_key == "sk-local"
    assert load_ai_settings(settings, None).model == "gpt-5.6-luna"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert load_ai_settings(settings, "bogus").api_key == "sk-env"
    assert "api_key" not in load_ai_settings(settings, None).public()


def test_api_flow(tmp_path):
    settings, storage, cid = seed(tmp_path, [BASE, BASE + 60])
    llm = FakeLLM()
    app = create_app(settings, llm_factory=lambda _ai: llm)
    with TestClient(app, base_url="http://127.0.0.1:18765") as client:
        body = {"conversation_id": cid, "start_at": BASE, "end_at": BASE + HOUR, "range_label": "今天"}
        preview = client.post("/api/summaries/preview", json=body).json()
        assert (preview["message_count"], preview["window_count"], preview["ai"]["configured"]) == (2, 1, True)
        assert "sk-test" not in json.dumps(preview)
        created = client.post("/api/summaries", json=body).json()
        for _ in range(200):
            summary = client.get(f"/api/summaries/{created['id']}").json()
            if summary["status"] not in ("queued", "running"):
                break
        assert summary["status"] == "success"
        topic_id = summary["topics"][0]["id"]
        assert len(client.get(f"/api/summary-topics/{topic_id}/messages").json()) == 2
        assert client.get("/api/summaries", params={"conversation_id": cid}).json()[0]["topic_count"] == 1
        assert client.put("/api/ai/settings", json={"model_tier": "bogus"}).status_code == 400
        assert client.put("/api/ai/settings", json={"model_tier": "high"}).json()["model"] == "gpt-5.6-terra"
        assert client.delete(f"/api/summaries/{created['id']}").status_code == 200
        assert client.get(f"/api/summaries/{created['id']}").status_code == 404


def test_summaries_are_kept_and_delete_never_touches_messages(tmp_path):
    service, storage, cid = make(tmp_path, [BASE, BASE + 60], FakeLLM())
    first = service.start(cid, BASE, BASE + HOUR, "第一次", wait=True)
    second = service.start(cid, BASE, BASE + HOUR, "重新生成", wait=True)  # regenerate adds, never replaces
    assert [s["id"] for s in storage.list_topic_summaries()] == [second, first]  # newest generated first
    with storage.connect() as conn:
        messages_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    assert storage.delete_topic_summary(first)
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == messages_before == 2
        assert conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 2
        # The deleted summary's topics and links are gone; nothing else is.
        assert conn.execute(
            "SELECT COUNT(*) FROM summary_topics WHERE summary_id=?", (first,)
        ).fetchone()[0] == 0
    remaining = storage.get_topic_summary(second)
    assert remaining["topics"] and len(storage.topic_source_messages(remaining["topics"][0]["id"])) == 2
    assert [s["id"] for s in storage.list_topic_summaries()] == [second]
