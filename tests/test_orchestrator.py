import math
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chattrace.app import create_app
from chattrace.orchestrator import ArchiveOrchestrator, Timeouts

from test_sync import make_service


class FakeEnvironment:
    """Simulated clock and WeChat process.

    WeChat appears ``start_delay`` seconds after launch, starts writing at
    ``login_at`` (absolute time) and keeps changing its files every second
    until ``writes_until``.
    """

    def __init__(self, *, running=False, exe="C:/Weixin/Weixin.exe", version="4.1.15.13",
                 start_delay=4.0, login_at=20.0, writes_until=80.0):
        self.t = 0.0
        self.running = running
        self.exe = exe
        self.version = version
        self.start_delay = start_delay
        self.login_at = login_at
        self.writes_until = writes_until
        self.launched_at = None
        self.exit_at = math.inf
        self.on_sleep = None

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.on_sleep:
            self.on_sleep(self)

    def find_executable(self):
        return self.exe

    def executable_version(self, path):
        return self.version

    def launch(self, path):
        self.launched_at = self.t

    def is_running(self):
        if self.t >= self.exit_at:
            return False
        if self.running:
            return True
        return self.launched_at is not None and self.t >= self.launched_at + self.start_delay

    def fingerprint(self):
        if self.t < self.login_at:
            return {"message_0.db": (0, 0)}
        return {"message_0.db": (int(min(self.t, self.writes_until)), 0)}


FAST = Timeouts(launch=60, login=600, stability=1200, settle=15, poll=2)


def make_orchestrator(tmp_path: Path, env: FakeEnvironment):
    storage, service, reader, _ = make_service(tmp_path)
    return ArchiveOrchestrator(storage, service.settings, service, environment=env, timeouts=FAST), storage


def test_launch_wait_archive_and_rescan(tmp_path: Path):
    env = FakeEnvironment()
    orchestrator, storage = make_orchestrator(tmp_path, env)
    orchestrator.start(45, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "done", state["message"]
    assert state["launched_by_chattrace"] is True and state["was_running"] is False
    assert env.launched_at == 0
    # Archiving only began after 45 quiet seconds following the last write.
    assert env.t >= env.writes_until + 45
    first = storage.get_job(state["archive_job_id"])
    second = storage.get_job(state["rescan_job_id"])
    assert (first["status"], first["inserted_count"]) == ("success", 2)
    assert (second["status"], second["inserted_count"]) == ("success", 0)
    assert "托盘" in state["message"]
    assert [e["text"] for e in state["events"]][0].startswith("启动微信")


def test_already_running_is_not_launched_or_closed(tmp_path: Path):
    env = FakeEnvironment(running=True, login_at=0)
    orchestrator, _ = make_orchestrator(tmp_path, env)
    orchestrator.start(30, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "done"
    assert env.launched_at is None
    assert state["was_running"] is True and state["launched_by_chattrace"] is False
    assert "不会关闭" in state["message"]


def test_incompatible_version_is_not_launched(tmp_path: Path):
    env = FakeEnvironment(version="4.2.0.1")
    orchestrator, storage = make_orchestrator(tmp_path, env)
    orchestrator.start(45, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "failed" and "白名单" in state["message"]
    assert state["reached"] == "launching"
    assert env.launched_at is None
    assert storage.list_jobs() == []


def test_missing_executable(tmp_path: Path):
    env = FakeEnvironment(exe=None)
    orchestrator, _ = make_orchestrator(tmp_path, env)
    orchestrator.start(45, wait=True)
    assert "CHATTRACE_WEIXIN_EXE" in orchestrator.snapshot()["message"]


def test_login_timeout_leaves_wechat_running(tmp_path: Path):
    env = FakeEnvironment(login_at=math.inf)
    orchestrator, storage = make_orchestrator(tmp_path, env)
    orchestrator.start(45, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "failed" and "未登录" in state["message"]
    assert env.is_running() and storage.list_jobs() == []


def test_continuous_writes_time_out(tmp_path: Path):
    env = FakeEnvironment(writes_until=math.inf)
    orchestrator, storage = make_orchestrator(tmp_path, env)
    orchestrator.start(45, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "failed" and "持续写入" in state["message"]
    assert storage.list_jobs() == []


def test_wechat_exit_during_settle_skips_rescan(tmp_path: Path):
    env = FakeEnvironment()
    orchestrator, storage = make_orchestrator(tmp_path, env)
    original_start = orchestrator.sync_service.start

    def start_then_quit(**kwargs):
        job_id = original_start(**kwargs)
        env.exit_at = env.t + 5  # user quits WeChat right after the first archive
        return job_id

    orchestrator.sync_service.start = start_then_quit
    orchestrator.start(45, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "done" and "跳过补偿扫描" in state["message"]
    assert state["rescan_job_id"] is None
    assert len(storage.list_jobs()) == 1


def test_wechat_exit_while_waiting_fails(tmp_path: Path):
    env = FakeEnvironment()
    env.exit_at = 50
    orchestrator, storage = make_orchestrator(tmp_path, env)
    orchestrator.start(45, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "failed" and "微信已退出" in state["message"]
    assert storage.list_jobs() == []


def test_cancel_while_waiting(tmp_path: Path):
    env = FakeEnvironment()
    orchestrator, storage = make_orchestrator(tmp_path, env)
    env.on_sleep = lambda e: e.t >= 40 and orchestrator.cancel()
    orchestrator.start(45, wait=True)
    state = orchestrator.snapshot()
    assert state["phase"] == "cancelled" and state["reached"] == "wechat_writing"
    assert storage.list_jobs() == []
    assert env.is_running()


def test_start_validation(tmp_path: Path):
    env = FakeEnvironment()
    orchestrator, storage = make_orchestrator(tmp_path, env)
    with pytest.raises(ValueError, match="稳定等待"):
        orchestrator.start(5, wait=True)
    conversation = storage.list_conversations()[0]
    storage.configure_conversation(conversation["id"], enabled=False, history_mode="all", history_start_at=None)
    with pytest.raises(ValueError, match="尚未选择"):
        orchestrator.start(45, wait=True)


def test_api_runs_flow(tmp_path: Path):
    storage, service, _, _ = make_service(tmp_path)
    env = FakeEnvironment()
    app = create_app(service.settings, sync_service=service, orchestrator_env=env)
    app.state.orchestrator.timeouts = FAST
    with TestClient(app, base_url="http://127.0.0.1:18765") as client:
        assert client.get("/api/orchestration").json()["phase"] == "idle"
        assert client.post("/api/orchestration/start", json={"quiet_seconds": 5}).status_code == 422
        started = client.post("/api/orchestration/start", json={"quiet_seconds": 30})
        assert started.status_code == 200
        for _ in range(500):
            state = client.get("/api/orchestration").json()
            if not state["active"]:
                break
            time.sleep(0.01)
        assert state["phase"] == "done", state
        assert client.post("/api/orchestration/cancel").status_code == 404
