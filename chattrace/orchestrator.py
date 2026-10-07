"""Semi-automatic WeChat session: launch → wait for local writes to settle → archive.

The chat databases stay read-only throughout. Starting WeChat is application
lifecycle control and only happens when the user asks for it. ChatTrace never
logs in on the user's behalf and never closes WeChat: WeChat offers no
reliable graceful-exit signal (WM_CLOSE only hides it to the tray) and a
forced kill could interrupt database writes.

"Stable" means the local database and WAL files have not changed for a while.
It is an engineering heuristic, not confirmation that WeChat has fetched all
messages from the server.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import Settings
from .storage import Storage, utc_now
from .sync_service import SyncService
from .wechat_adapter import WeChatReader, _load_readonly_db_module, _weixin_processes

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None


logger = logging.getLogger("chattrace.orchestrator")

PHASE_LABELS = {
    "idle": "未开始",
    "launching": "启动微信",
    "waiting_login": "等待登录",
    "wechat_writing": "微信写入中",
    "stable": "数据暂时稳定",
    "archiving": "聊迹归档中",
    "settling": "等待补扫",
    "rescanning": "补偿扫描",
    "done": "归档完成",
    "failed": "未完成",
    "cancelled": "已取消",
}
ACTIVE_PHASES = {"launching", "waiting_login", "wechat_writing", "stable", "archiving", "settling", "rescanning"}
QUIET_SECONDS_RANGE = (15, 600)


class Environment(Protocol):
    def find_executable(self) -> str | None: ...
    def executable_version(self, path: str) -> str | None: ...
    def is_running(self) -> bool: ...
    def launch(self, path: str) -> None: ...
    def fingerprint(self) -> dict[str, tuple[int, int]]: ...
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class WindowsEnvironment:
    """The real machine: registry, processes and WeChat's own database files."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def find_executable(self) -> str | None:
        candidates: list[str] = []
        override = os.environ.get("CHATTRACE_WEIXIN_EXE")
        if override:
            candidates.append(override)
        for process in _weixin_processes():
            try:
                candidates.append(process.exe())
            except Exception:
                continue
        candidates.extend(self._registry_candidates())
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(os.path.join(base, "Tencent", "Weixin", "Weixin.exe"))
        for candidate in candidates:
            path = candidate.strip().strip('"')
            # Only WeChat 4.x (Weixin.exe); an old WeChat 3.x (WeChat.exe) may coexist.
            if os.path.basename(path).lower() == "weixin.exe" and os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _registry_candidates() -> list[str]:
        if winreg is None:
            return []
        found: list[str] = []
        lookups = [
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Weixin", "InstallPath"),
        ]
        uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Weixin"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            lookups.append((hive, uninstall, "InstallLocation"))
        for hive, key, value in lookups:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    raw, _ = winreg.QueryValueEx(handle, value)
            except OSError:
                continue
            found.append(os.path.join(str(raw).strip().strip('"'), "Weixin.exe"))
        return found

    def executable_version(self, path: str) -> str | None:
        return WeChatReader._file_version(path)

    def is_running(self) -> bool:
        return bool(_weixin_processes())

    def launch(self, path: str) -> None:
        # Same as the user double-clicking WeChat: detached from ChatTrace, so
        # WeChat keeps running if ChatTrace exits.
        os.startfile(path)  # type: ignore[attr-defined]

    def fingerprint(self) -> dict[str, tuple[int, int]]:
        """``{path: (mtime_ns, size)}`` of WeChat's database and WAL files.

        Only metadata is read. ``-shm`` files are skipped because readers also
        touch them.
        """
        try:
            module = _load_readonly_db_module()
            db_dir = module.auto_detect_db_dir()
            accounts = module._find_account_dirs(db_dir) if db_dir else []
        except Exception:
            return {}
        result: dict[str, tuple[int, int]] = {}
        for account in accounts:
            for root, _, files in os.walk(Path(account) / "db_storage"):
                for name in files:
                    if not name.endswith((".db", ".db-wal")):
                        continue
                    path = os.path.join(root, name)
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    result[path] = (stat.st_mtime_ns, stat.st_size)
        return result

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Timeouts:
    launch: float = 60
    login: float = 10 * 60
    stability: float = 20 * 60
    settle: float = 15
    poll: float = 2


class _Stop(Exception):
    """Ends the run early with a final phase and message."""

    def __init__(self, phase: str, message: str):
        super().__init__(message)
        self.phase = phase
        self.message = message


class _WeChatExited(Exception):
    pass


class ArchiveOrchestrator:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        sync_service: SyncService,
        environment: Environment | None = None,
        timeouts: Timeouts = Timeouts(),
    ):
        self.storage = storage
        self.settings = settings
        self.sync_service = sync_service
        self.env = environment or WindowsEnvironment(settings)
        self.timeouts = timeouts
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = self._initial_state()

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "phase": "idle",
            "reached": None,
            "started_at": None,
            "finished_at": None,
            "was_running": None,
            "launched_by_chattrace": False,
            "quiet_seconds": None,
            "quiet_remaining": None,
            "last_write_at": None,
            "archive_job_id": None,
            "rescan_job_id": None,
            "message": "",
            "events": [],
        }

    # -- public API ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["events"] = list(self._state["events"])
        state["phase_label"] = PHASE_LABELS[state["phase"]]
        state["active"] = state["phase"] in ACTIVE_PHASES
        return state

    def start(self, quiet_seconds: int = 45, *, wait: bool = False) -> dict[str, Any]:
        low, high = QUIET_SECONDS_RANGE
        if not low <= quiet_seconds <= high:
            raise ValueError(f"稳定等待时间需在 {low}～{high} 秒之间")
        with self._lock:
            if self._state["phase"] in ACTIVE_PHASES:
                raise ValueError("启动并归档流程已在进行中")
            if not self.storage.selected_conversations():
                raise ValueError("尚未选择任何需要同步的会话")
            if self.storage.active_job_id() is not None:
                raise ValueError("已有同步任务正在运行")
            self._cancel.clear()
            self._state = self._initial_state()
            self._state.update(
                phase="launching", reached="launching", started_at=utc_now(), quiet_seconds=quiet_seconds
            )
        if wait:
            self._run(quiet_seconds)
        else:
            self._thread = threading.Thread(
                target=self._run, args=(quiet_seconds,), daemon=True, name="chattrace-orchestrator"
            )
            self._thread.start()
        return self.snapshot()

    def cancel(self) -> bool:
        with self._lock:
            if self._state["phase"] not in ACTIVE_PHASES:
                return False
            job_id = self._state["archive_job_id"] if self._state["phase"] == "archiving" else (
                self._state["rescan_job_id"] if self._state["phase"] == "rescanning" else None
            )
        self._cancel.set()
        if job_id is not None:
            self.sync_service.cancel(job_id)
        return True

    # -- state helpers -------------------------------------------------------

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _event(self, text: str) -> None:
        with self._lock:
            events = self._state["events"]
            events.append({"at": utc_now(), "text": text})
            del events[:-50]

    def _phase(self, phase: str, text: str) -> None:
        # ``reached`` keeps the last working phase so a failed or cancelled
        # run can show where it stopped.
        self._update(phase=phase, reached=phase, quiet_remaining=None)
        self._event(text)
        logger.info("启动并归档流程: %s", phase)

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Stop("cancelled", "已取消。微信保持当前状态，已写入的归档不受影响。")

    # -- flow ----------------------------------------------------------------

    def _run(self, quiet_seconds: int) -> None:
        try:
            self._prepare_wechat()
            self._phase("wechat_writing", f"等待本地数据库连续 {quiet_seconds} 秒无新写入")
            self._wait_quiet(quiet_seconds, self.timeouts.stability)
            self._phase("stable", "数据暂时稳定（仅表示本机暂无新写入，不代表云端消息已全部同步）")

            self._phase("archiving", "开始归档已选会话")
            first = self._sync("archive_job_id")
            self._phase("settling", f"归档完成（任务 #{first['id']}），等待 {int(self.timeouts.settle)} 秒后补扫")
            try:
                self._wait_quiet(self.timeouts.settle, self.timeouts.stability)
            except _WeChatExited:
                raise _Stop("done", self._done_message("微信已退出，跳过补偿扫描。"))

            self._phase("rescanning", "补偿扫描，收录归档期间新写入的消息")
            second = self._sync("rescan_job_id")
            note = "" if second["status"] == "success" else f"补偿扫描状态：{second['status']}。"
            raise _Stop("done", self._done_message(note))
        except _Stop as stop:
            self._finish(stop.phase, stop.message)
        except _WeChatExited:
            self._finish("failed", "微信已退出，流程中止。已写入的归档不受影响。")
        except Exception as exc:
            logger.exception("启动并归档流程异常")
            self._finish("failed", f"流程异常：{exc}")

    def _prepare_wechat(self) -> None:
        compatible = self.settings.compatible_versions
        was_running = self.env.is_running()
        self._update(was_running=was_running)
        if was_running:
            self._event("微信已在运行，跳过启动和登录等待；聊迹不会关闭它")
            return

        path = self.env.find_executable()
        if not path:
            raise _Stop("failed", "未找到 Weixin.exe。可设置环境变量 CHATTRACE_WEIXIN_EXE 指向微信程序。")
        version = self.env.executable_version(path)
        if version not in compatible:
            # Checked before launching: an unverified version must not be started for sync.
            raise _Stop("failed", f"微信 {version or '未知版本'} 尚未进入兼容白名单，未启动微信。")

        baseline = self.env.fingerprint()
        self._phase("launching", f"启动微信 {version}")
        self.env.launch(path)
        self._update(launched_by_chattrace=True)
        deadline = self.env.now() + self.timeouts.launch
        while not self.env.is_running():
            self._check_cancel()
            if self.env.now() >= deadline:
                raise _Stop("failed", "微信启动超时，未检测到 Weixin.exe 进程。")
            self.env.sleep(self.timeouts.poll)

        self._phase("waiting_login", "请在微信中完成登录（扫码或手机确认），聊迹不会代替登录")
        deadline = self.env.now() + self.timeouts.login
        while self.env.fingerprint() == baseline:
            self._check_cancel()
            if not self.env.is_running():
                raise _WeChatExited()
            if self.env.now() >= deadline:
                minutes = int(self.timeouts.login // 60)
                raise _Stop("failed", f"{minutes} 分钟内未检测到微信写入本地数据库，可能尚未登录。微信保持运行。")
            self.env.sleep(self.timeouts.poll)
        self._update(last_write_at=utc_now())
        self._event("检测到微信开始写入本地数据库")

    def _wait_quiet(self, quiet_seconds: float, timeout: float) -> None:
        start = last_change = self.env.now()
        last = self.env.fingerprint()
        while True:
            self._check_cancel()
            elapsed_quiet = self.env.now() - last_change
            self._update(quiet_remaining=max(0, int(round(quiet_seconds - elapsed_quiet))))
            if elapsed_quiet >= quiet_seconds:
                return
            if self.env.now() - start >= timeout:
                minutes = int(timeout // 60)
                raise _Stop("failed", f"微信持续写入超过 {minutes} 分钟，无法判断稳定。微信保持运行，可稍后重试。")
            self.env.sleep(self.timeouts.poll)
            if not self.env.is_running():
                raise _WeChatExited()
            current = self.env.fingerprint()
            if current != last:
                last, last_change = current, self.env.now()
                self._update(last_write_at=utc_now())

    def _sync(self, field: str) -> dict[str, Any]:
        self._check_cancel()
        try:
            job_id = self.sync_service.start(wait=True, on_created=lambda jid: self._update(**{field: jid}))
        except ValueError as exc:
            raise _Stop("failed", f"无法开始归档：{exc}")
        job = self.storage.get_job(job_id)
        if job["status"] == "cancelled":
            raise _Stop("cancelled", "已取消。已写入的归档不受影响。")
        if job["status"] == "failed":
            raise _Stop("failed", f"归档任务 #{job_id} 失败：{job.get('error_message') or '详见同步记录'}")
        return job

    def _done_message(self, note: str = "") -> str:
        with self._lock:
            launched = self._state["launched_by_chattrace"]
        tail = (
            "微信由聊迹启动；如不再使用，请从任务栏托盘菜单选择“退出微信”。"
            if launched
            else "微信在操作前已在运行，聊迹不会关闭它。"
        )
        return f"归档完成。{note}{tail}"

    def _finish(self, phase: str, message: str) -> None:
        self._update(phase=phase, message=message, finished_at=utc_now(), quiet_remaining=None)
        self._event(message)
        logger.info("启动并归档流程结束: %s", phase)
