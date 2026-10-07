from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chattrace.app import create_app
from chattrace.wechat_adapter import WeChatStatus

from fakes import make_settings


class StatusReader:
    def status(self):
        return WeChatStatus(False, False, None, None, False, False, "微信未运行")

    def list_sessions(self):
        return [{"username": "chat", "display_name": "Chat", "last_time": 1}]

    def close(self):
        pass


@pytest.fixture
def client(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings, reader_factory=StatusReader)
    app.state.sync_service.status_check = lambda: StatusReader().status()
    with TestClient(app, base_url="http://127.0.0.1:18765") as test_client:
        yield test_client


def test_rejects_foreign_host(client):
    # DNS rebinding: a page on evil.example resolved to 127.0.0.1.
    response = client.get("/api/messages", headers={"host": "evil.example:18765"})
    assert response.status_code == 400


def test_rejects_cross_site_requests(client):
    assert client.post("/api/sync", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/sync", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get("/api/health", headers={"origin": "http://127.0.0.1:18765"}).status_code == 200


def test_since_mode_requires_start(client):
    client.post("/api/conversations/scan")
    conversation_id = client.get("/api/conversations").json()[0]["id"]
    bad = client.put(f"/api/conversations/{conversation_id}", json={"enabled": True, "history_mode": "since"})
    assert bad.status_code == 422
    good = client.put(
        f"/api/conversations/{conversation_id}",
        json={"enabled": True, "history_mode": "since", "history_start_at": 1_700_000_000},
    )
    assert good.status_code == 200 and good.json()["history_start_at"] == 1_700_000_000


def test_sync_refused_without_wechat_creates_no_job(client):
    client.post("/api/conversations/scan")
    conversation_id = client.get("/api/conversations").json()[0]["id"]
    client.put(f"/api/conversations/{conversation_id}", json={"enabled": True})
    response = client.post("/api/sync")
    assert response.status_code == 400 and response.json()["detail"] == "微信未运行"
    assert client.get("/api/jobs").json() == []


def test_startup_marks_stale_jobs_interrupted(tmp_path: Path):
    settings = make_settings(tmp_path)
    first = create_app(settings, reader_factory=StatusReader)
    with TestClient(first, base_url="http://127.0.0.1:18765"):
        storage = first.state.storage
        storage.upsert_conversations([{"username": "chat", "display_name": "Chat"}])
        job_id = storage.create_sync_job("manual", storage.list_conversations())
        with storage.connect() as conn:
            conn.execute("UPDATE sync_jobs SET status='running' WHERE id=?", (job_id,))
    second = create_app(settings, reader_factory=StatusReader)
    with TestClient(second, base_url="http://127.0.0.1:18765") as test_client:
        assert test_client.get("/api/jobs").json()[0]["status"] == "interrupted"


def test_unselected_conversation_archive_stays_queryable(client):
    client.post("/api/conversations/scan")
    conversation_id = client.get("/api/conversations").json()[0]["id"]
    client.put(f"/api/conversations/{conversation_id}", json={"enabled": True})
    storage = client.app.state.storage
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO messages(conversation_id, source_key, sort_seq, content, content_hash,"
            " source_created_at, archived_at, updated_at) VALUES(?, 'k', 1, '保留的归档', 'h', 100, 'n', 'n')",
            (conversation_id,),
        )
    client.put(f"/api/conversations/{conversation_id}", json={"enabled": False})
    result = client.get("/api/messages", params={"conversation_id": conversation_id}).json()
    assert result["total"] == 1
    preview = client.post(
        "/api/summaries/preview",
        json={"conversation_id": conversation_id, "start_at": 0, "end_at": 200},
    ).json()
    assert preview["message_count"] == 1


def test_ui_files_are_revalidated(client):
    # An updated UI must not hide behind a heuristically cached old copy.
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/health").headers


def test_untrack_endpoints(client):
    client.post("/api/conversations/scan")  # StatusReader session: last_time=1 (very old)
    conversation_id = client.get("/api/conversations").json()[0]["id"]
    assert client.get("/api/conversations/inactive", params={"days": 30}).json() == {"count": 1}
    assert client.get("/api/conversations/inactive", params={"days": 1}).status_code == 422
    assert client.post("/api/conversations/hide-inactive", json={"days": 30}).json() == {"hidden": 1}
    assert client.get("/api/conversations").json()[0]["hidden"] == 1
    restore = client.post("/api/conversations/hide", json={"conversation_ids": [conversation_id], "hidden": False})
    assert restore.json() == {"changed": 1}
    assert client.post("/api/conversations/hide", json={"conversation_ids": []}).status_code == 422
