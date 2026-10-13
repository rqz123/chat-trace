from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from typing import Any, Callable, Iterator

import psutil

try:
    import win32api
except ImportError:  # pragma: no cover - Windows runtime dependency
    win32api = None

try:
    import win32crypt
except ImportError:  # pragma: no cover - Windows runtime dependency
    win32crypt = None

from .config import Settings


logger = logging.getLogger("chattrace.wechat")


class WeChatUnavailable(RuntimeError):
    pass


class UnsupportedWeChatVersion(RuntimeError):
    pass


@dataclass
class WeChatStatus:
    running: bool
    logged_in: bool
    version: str | None
    executable: str | None
    database_dir_found: bool
    compatible: bool
    message: str
    # The same WeChat process was logged in earlier and is back at the login window.
    logged_out: bool = False

    @property
    def can_sync(self) -> bool:
        return self.running and self.logged_in and self.compatible and self.database_dir_found

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Key cache at rest
# ---------------------------------------------------------------------------
# Upstream stores database keys as plaintext JSON (plus a .bak and a second
# "stable" copy under %LOCALAPPDATA%\wechatauto_keys). Those keys decrypt the
# user's entire WeChat history, so ChatTrace keeps a single copy protected with
# Windows DPAPI (bound to the current Windows user).

_KEY_MAGIC = b"CHATTRACE-DPAPI-1\n"


def read_key_file(path: str | os.PathLike) -> dict[str, bytes]:
    """Read a key cache in either DPAPI or legacy plaintext form."""
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError:
        return {}
    try:
        if raw.startswith(_KEY_MAGIC):
            if win32crypt is None:
                return {}
            _, raw = win32crypt.CryptUnprotectData(raw[len(_KEY_MAGIC):], None, None, None, 0)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    keys: dict[str, bytes] = {}
    for rel, hexkey in data.items():
        try:
            keys[rel] = bytes.fromhex(hexkey)
        except (ValueError, TypeError):
            continue
    return keys


def write_key_file(path: str | os.PathLike, keys: dict[str, bytes]) -> None:
    if not keys:
        return
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({k: v.hex() for k, v in keys.items()}).encode("utf-8")
    if win32crypt is not None:
        payload = _KEY_MAGIC + win32crypt.CryptProtectData(
            payload, "ChatTrace keys", None, None, None, 0
        )
    else:  # pragma: no cover - non-Windows development only
        logger.warning("win32crypt 不可用，密钥缓存将以明文保存")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)


def seal_key_file(path: str | os.PathLike) -> bool:
    """Convert a legacy plaintext key cache (and its .bak) to DPAPI form.

    Returns True when a plaintext file was converted or removed.
    """
    target = pathlib.Path(path)
    backup = target.with_name(target.name + ".bak")
    changed = False
    keys: dict[str, bytes] = {}
    if backup.exists():
        keys.update(read_key_file(backup))
    main_plain = target.exists() and not target.read_bytes().startswith(_KEY_MAGIC)
    keys.update(read_key_file(target))
    if main_plain or (backup.exists() and keys):
        write_key_file(target, keys)
        changed = True
    if backup.exists():
        backup.unlink()
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Upstream module loading
# ---------------------------------------------------------------------------


def _weixin_processes() -> list[psutil.Process]:
    processes = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (process.info.get("name") or "").lower() == "weixin.exe":
                processes.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return processes


# Main Weixin.exe process last seen logged in. Readers are created per request,
# so this lives at module level.
_logged_in_main_pid: int | None = None


def login_state(processes: list[psutil.Process]) -> tuple[bool, bool]:
    """``(logged_in, logged_out)`` from the Weixin.exe process tree.

    At the login window WeChat runs a single Weixin.exe. After login it starts
    helper Weixin.exe children (wxutility, wxocr, xplayer, ...), which exit
    again on logout while the main process stays. Only process ids and parents
    are read.
    """
    global _logged_in_main_pid
    pids = {p.pid for p in processes}
    mains: set[int] = set()
    helpers = 0
    for process in processes:
        try:
            parent = process.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if parent in pids:
            helpers += 1
        else:
            mains.add(process.pid)
    if helpers:
        _logged_in_main_pid = next(iter(mains), None)
        return True, False
    logged_out = _logged_in_main_pid is not None and _logged_in_main_pid in mains
    if not logged_out:
        _logged_in_main_pid = None
    return False, logged_out


def _install_patches(module) -> None:
    """Adjust the private copy of the upstream module.

    The module is loaded under a private name, so these class-level changes
    affect only ChatTrace. Each one changes discovery or persistence; the
    database module still opens WeChat with VM_READ | QUERY_INFORMATION only.
    """
    db_class = module.WeChatDB

    # Upstream discovers PIDs through ``tasklist``, which some managed Windows
    # environments deny. Discover live on every call so a restarted WeChat is
    # never matched against stale PIDs.
    db_class._find_weixin_pids = lambda _self: [p.pid for p in _weixin_processes()]

    def _load_key_cache(_self, path):
        return read_key_file(path)

    def _save_keys(self):
        write_key_file(self.keys_file, self._keys)

    db_class._load_key_cache = _load_key_cache
    db_class._save_keys = _save_keys
    # No second plaintext copy under %LOCALAPPDATA%\wechatauto_keys.
    db_class._stable_key_dirs = lambda _self: []

    # Upstream falls back to %USERPROFILE%\Documents, which misses a Documents
    # folder redirected elsewhere (e.g. OneDrive) when WeChat's config only says
    # "MyDocument:". Callers resolve the function through module globals.
    upstream_detect = module.auto_detect_db_dir

    def auto_detect_db_dir():
        override = os.environ.get("CHATTRACE_WECHAT_DATA_DIR")
        if override:
            return module._locate_account_root(override)
        found = upstream_detect()
        if found:
            return found
        documents = _documents_folder()
        return module._locate_account_root(documents) if documents else None

    module.auto_detect_db_dir = auto_detect_db_dir


def _documents_folder() -> str | None:
    """The shell's real Documents folder, honouring folder redirection."""
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    CSIDL_PERSONAL = 5
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buffer) != 0:
        return None
    return buffer.value or None


def _load_readonly_db_module():
    existing = sys.modules.get("chattrace._wechat_db")
    if existing is not None:
        return existing

    source = os.environ.get("CHATTRACE_WECHATAUTO_SOURCE")
    if source:
        db_path = pathlib.Path(source).resolve() / "wechatauto" / "db.py"
    else:
        spec = importlib.util.find_spec("wechatauto")
        if spec is None or not spec.submodule_search_locations:
            raise WeChatUnavailable(
                "未安装只读微信适配器。请安装 chattrace[wechat]，"
                "或设置 CHATTRACE_WECHATAUTO_SOURCE。"
            )
        db_path = pathlib.Path(next(iter(spec.submodule_search_locations))) / "db.py"
    if not db_path.is_file():
        raise WeChatUnavailable(f"未找到微信只读数据库模块: {db_path}")

    package = types.ModuleType("wechatauto")
    package.__path__ = [str(db_path.parent)]
    logger_module = types.ModuleType("wechatauto.logger")
    logger_module.wxlog = logging.getLogger("chattrace.wechat")
    sys.modules["wechatauto"] = package
    sys.modules["wechatauto.logger"] = logger_module

    module_spec = importlib.util.spec_from_file_location("chattrace._wechat_db", db_path)
    if module_spec is None or module_spec.loader is None:
        raise WeChatUnavailable("无法加载微信只读数据库模块")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["chattrace._wechat_db"] = module
    module_spec.loader.exec_module(module)
    _install_patches(module)
    return module


# ---------------------------------------------------------------------------
# Message reading
# ---------------------------------------------------------------------------

# Group messages from other members are stored as "<username>:\n<text>".
_GROUP_PREFIX = re.compile(r"^([^\s:]{1,64}):\n")
_USERNAME_LIKE = re.compile(
    r"^(wxid_[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9_-]{5,19}|[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+)$"
)


def split_group_content(content: str, sender_username: str | None) -> tuple[str, str | None]:
    """Strip the sender prefix from a group message body.

    Returns ``(body, prefix_username)``; ``prefix_username`` is None when no
    prefix was removed. When the sender is resolved from the shard's Name2Id
    table, the prefix must equal it exactly. Only when the sender is unknown is
    a prefix that looks like a WeChat username trusted. Either way, user text
    such as "备注:\\n..." or "zhangsan_01:\\n..." from another sender is kept.
    """
    match = _GROUP_PREFIX.match(content)
    if not match:
        return content, None
    prefix = match.group(1)
    if sender_username:
        trusted = prefix == sender_username
    else:
        trusted = bool(_USERNAME_LIKE.match(prefix))
    if trusted:
        return content[match.end():], prefix
    return content, None


@dataclass(frozen=True)
class MessageWindow:
    """Which source rows a sync pass reads.

    ``history_cutoff``: only rows created at or after this time (0 = all).
    ``after_sort_seq``: incremental mode; rows with a newer sort_seq *or*
    created inside the lookback window (``lookback_from``) are read again.
    """

    history_cutoff: int = 0
    after_sort_seq: int | None = None
    lookback_from: int = 0

    def where(self) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.history_cutoff:
            clauses.append("m.create_time >= ?")
            params.append(self.history_cutoff)
        if self.after_sort_seq is not None:
            clauses.append("(m.sort_seq > ? OR m.create_time >= ?)")
            params.extend([self.after_sort_seq, self.lookback_from])
        return clauses, params


ContentDecoder = Callable[[Any, Any, Any], tuple[str, str]]

_MSG_COLUMNS = (
    "m.local_id, m.local_type, m.real_sender_id, m.create_time, "
    "m.message_content, m.compress_content, m.server_id, m.sort_seq"
)


def _plain_decoder(local_type: Any, content: Any, _compressed: Any) -> tuple[str, str]:
    name = "文本" if local_type == 1 else str(local_type)
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return name, str(content or "")


def upstream_decoder(dbmod) -> ContentDecoder:
    """Content decoding identical to upstream ``_msg_row_to_dict``."""
    db_class = dbmod.WeChatDB

    def decode(local_type: Any, content: Any, compressed: Any) -> tuple[str, str]:
        mtype = db_class._msg_type_name(local_type)
        if isinstance(content, bytes):
            content = db_class._friendly_content(content, mtype)
        placeholder = "[%s]" % mtype
        if isinstance(content, str) and not content.strip():
            content = placeholder
        if content == placeholder and isinstance(compressed, bytes) and compressed:
            text = db_class._friendly_content(compressed, mtype)
            if text != placeholder:
                content = text
        return str(mtype), str(content or "")

    return decode


class ConversationSource:
    """Read-only view over every shard table of one conversation.

    Pages with a ``sort_seq`` keyset instead of OFFSET: upstream's offset
    paging re-reads ``offset + limit`` rows from every shard per page, which is
    quadratic for large groups. Rows sharing the boundary ``sort_seq`` are
    always returned together, so a page never splits a tie group.
    """

    def __init__(
        self,
        tables: list[tuple[sqlite3.Connection, str]],
        *,
        username: str,
        self_username: str = "",
        decode: ContentDecoder = _plain_decoder,
    ):
        self.tables = tables
        self.username = username
        self.is_group = username.endswith("@chatroom")
        self.self_username = self_username
        self.decode = decode
        self._has_name2id: dict[int, bool] = {}

    def _name2id(self, conn: sqlite3.Connection) -> bool:
        key = id(conn)
        if key not in self._has_name2id:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'"
            ).fetchone()
            self._has_name2id[key] = row is not None
        return self._has_name2id[key]

    def _select(self, conn, table, window: MessageWindow, extra: list[str], extra_params,
                tail: str, tail_params) -> list[Any]:
        # The sender of each row lives in the *same shard's* Name2Id table.
        # Upstream resolves it through message_resource.SenderName2Id, which
        # does not line up with real_sender_id in WeChat 4.x.
        if self._name2id(conn):
            sender, join = "n.user_name", "LEFT JOIN Name2Id n ON n.rowid = m.real_sender_id"
        else:
            sender, join = "NULL", ""
        clauses, params = window.where()
        clauses = clauses + extra
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT {_MSG_COLUMNS}, {sender} AS sender_username FROM {table} m {join}{where} {tail}"
        return conn.execute(sql, [*params, *extra_params, *tail_params]).fetchall()

    def count(self, window: MessageWindow) -> int:
        total = 0
        for conn, table in self.tables:
            clauses, params = window.where()
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            row = conn.execute(f"SELECT COUNT(*) FROM {table} m{where}", params).fetchone()
            total += int(row[0] or 0)
        return total

    def page(
        self, window: MessageWindow, before_seq: int | None, limit: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Return one page (newest first) and the keyset for the next page."""
        extra, extra_params = ([], []) if before_seq is None else (["m.sort_seq < ?"], [before_seq])
        merged: list[tuple[Any, int]] = []
        for index, (conn, table) in enumerate(self.tables):
            rows = self._select(
                conn, table, window, extra, extra_params,
                "ORDER BY m.sort_seq DESC, m.local_id ASC LIMIT ?", [limit + 1],
            )
            merged.extend((row, index) for row in rows)
        merged.sort(key=lambda item: (-int(item[0]["sort_seq"]), item[1], item[0]["local_id"]))
        if len(merged) <= limit:
            return [self._to_message(row) for row, _ in merged], None

        boundary = int(merged[limit - 1][0]["sort_seq"])
        page = [item for item in merged if int(item[0]["sort_seq"]) > boundary]
        ties: list[tuple[Any, int]] = []
        for index, (conn, table) in enumerate(self.tables):
            rows = self._select(
                conn, table, window, ["m.sort_seq = ?"], [boundary],
                "ORDER BY m.local_id ASC", [],
            )
            ties.extend((row, index) for row in rows)
        ties.sort(key=lambda item: (item[1], item[0]["local_id"]))
        page.extend(ties)
        return [self._to_message(row) for row, _ in page], boundary

    def batches(self, window: MessageWindow, batch_size: int = 500) -> Iterator[list[dict[str, Any]]]:
        before: int | None = None
        while True:
            rows, before = self.page(window, before, batch_size)
            if rows:
                yield rows
            if before is None:
                return

    def _to_message(self, row) -> dict[str, Any]:
        mtype, content = self.decode(row["local_type"], row["message_content"], row["compress_content"])
        sender_username = row["sender_username"] or ""
        has_prefix = False
        if self.is_group and mtype == "文本":
            content, prefix = split_group_content(content, sender_username or None)
            if prefix:
                has_prefix = True
                sender_username = sender_username or prefix
        if self.self_username and sender_username:
            is_self = sender_username == self.self_username
        else:
            # Without a resolvable sender, an unprefixed group text is ours.
            is_self = self.is_group and mtype == "文本" and not has_prefix
        if is_self and not sender_username:
            sender_username = self.self_username
        server_id = row["server_id"]
        return {
            "local_id": row["local_id"],
            "type": mtype,
            "sender_id": row["real_sender_id"],
            "sender_username": sender_username,
            "is_self": is_self,
            "create_time": row["create_time"],
            "content": content,
            "sort_seq": row["sort_seq"],
            "server_id": str(server_id) if server_id not in (None, 0, "") else None,
        }


class WeChatReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._dbmod = None
        self._reader = None
        self._nicknames: dict[str, str] | None = None

    @staticmethod
    def _weixin_processes() -> list[psutil.Process]:
        return _weixin_processes()

    @staticmethod
    def _file_version(path: str | None) -> str | None:
        if not path or win32api is None:
            return None
        try:
            info = win32api.GetFileVersionInfo(path, "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:
            return None

    def status(self) -> WeChatStatus:
        processes = self._weixin_processes()
        if not processes:
            return WeChatStatus(False, False, None, None, False, False, "微信未运行")
        executable = None
        try:
            executable = processes[0].exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        version = self._file_version(executable)
        compatible = version in self.settings.compatible_versions
        database_found = False
        try:
            module = _load_readonly_db_module()
            database_found = bool(module.auto_detect_db_dir())
        except Exception:
            database_found = False
        logged_in, logged_out = login_state(processes)
        if version is None:
            message = "无法读取微信完整版本号"
        elif not compatible:
            message = f"微信 {version} 尚未进入兼容白名单"
        elif logged_out:
            message = "微信已退出登录，请重新登录后再同步"
        elif not logged_in:
            message = "微信已运行，但尚未登录"
        elif not database_found:
            message = "未找到微信本地数据库目录"
        else:
            message = "微信已登录，可以执行只读同步"
        return WeChatStatus(
            True,
            logged_in,
            version,
            executable,
            database_found,
            compatible,
            message,
            logged_out,
        )

    def _open(self):
        if self._reader is not None:
            return self._reader
        status = self.status()
        if not status.running or not status.logged_in or not status.database_dir_found:
            raise WeChatUnavailable(status.message)
        if not status.compatible:
            raise UnsupportedWeChatVersion(status.message)
        self._dbmod = _load_readonly_db_module()
        self._reader = self._dbmod.WeChatDB(
            workdir=str(self.settings.decrypted_dir),
            keys_file=str(self.settings.keys_file),
        )
        return self._reader

    @property
    def self_username(self) -> str:
        try:
            return str(self._open().wxid or "")
        except AttributeError:
            return ""

    def nicknames(self) -> dict[str, str]:
        if self._nicknames is None:
            try:
                self._nicknames = dict(self._open().nickname_map())
            except Exception:
                logger.warning("读取联系人昵称失败，将使用微信号显示发送者")
                self._nicknames = {}
        return self._nicknames

    def list_sessions(self, limit: int = 5000) -> list[dict[str, Any]]:
        reader = self._open()
        sessions = reader.get_sessions(limit=limit)
        names = self.nicknames()
        member_titles: dict[str, str] | None = None
        for session in sessions:
            username = str(session.get("username") or "")
            name = names.get(username) or ""
            if not name or name == username:
                if username.endswith("@chatroom"):
                    if member_titles is None:
                        member_titles = self._unnamed_group_titles()
                    name = member_titles.get(username, "")
            session["display_name"] = name or username
        return sessions

    def _unnamed_group_titles(self) -> dict[str, str]:
        """Titles for groups without a name, built the way WeChat shows them."""
        try:
            groups = self._open().get_groups()
        except Exception:
            logger.warning("读取群成员失败，没有群名的群将显示为群 ID")
            return {}
        me = self.self_username
        return {
            str(group["username"]): title
            for group in groups
            if (title := unnamed_group_title(group.get("members") or [], me))
        }

    @contextmanager
    def open_conversation(self, username: str) -> Iterator[ConversationSource | None]:
        """Open every shard of a conversation once for a consistent read.

        Yields None when the conversation has no message table.
        """
        reader = self._open()
        tables = reader._msg_conns(username)
        try:
            if not tables:
                yield None
                return
            yield ConversationSource(
                tables,
                username=username,
                self_username=self.self_username,
                decode=upstream_decoder(self._dbmod),
            )
        finally:
            closed: set[int] = set()
            for conn, _ in tables:
                if id(conn) not in closed:
                    closed.add(id(conn))
                    conn.close()

    def close(self) -> None:
        """Drop the reader and, unless configured otherwise, delete decrypted copies."""
        self._reader = None
        self._nicknames = None
        if not self.settings.keep_decrypted_cache:
            remove_decrypted_cache(self.settings)


def remove_decrypted_cache(settings: Settings) -> tuple[int, int]:
    """Delete decrypted database copies. Returns ``(removed, failed)``."""
    removed = failed = 0
    directory = settings.decrypted_dir
    if not directory.is_dir():
        return 0, 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            failed += 1
    if failed:
        logger.warning("有 %d 个解密副本暂时无法删除，将在下次同步后重试", failed)
    return removed, failed


UNNAMED_GROUP_TITLE_NAMES = 4


def unnamed_group_title(members: list[dict[str, Any]], self_username: str) -> str:
    """"A、B、C、D… (N)" from the other members, like WeChat's chat title.

    Returns "" when no other member has a usable name.
    """
    others = sorted(
        (m for m in members if m.get("username") and m.get("username") != self_username),
        key=lambda m: str(m["username"]),
    )
    labels = [str(m.get("remark") or m.get("nick_name") or m["username"]) for m in others]
    if not labels:
        return ""
    title = "、".join(labels[:UNNAMED_GROUP_TITLE_NAMES])
    if len(labels) > UNNAMED_GROUP_TITLE_NAMES:
        title += "…"
    return f"{title} ({len(members)})"


def server_message_key(conversation_id: int, server_id: Any) -> str | None:
    """Identity from WeChat's server message id, or None when there is none.

    local_id and real_sender_id are row ids in one computer's WeChat database,
    so an archive moved between computers must not depend on them.
    """
    server_id = str(server_id or "").strip()
    if not server_id or server_id == "0":
        return None
    raw = f"server|{conversation_id}|{server_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def message_source_key(conversation_id: int, message: dict[str, Any]) -> str:
    # Keep identity independent from content so a corrected/changed source row
    # updates the archived message instead of creating a duplicate. The inputs
    # must stay stable: changing them would duplicate every archived message
    # (schema v4 re-keyed existing rows when the server id was introduced).
    server_key = server_message_key(conversation_id, message.get("server_id"))
    if server_key:
        return server_key
    raw = "|".join(
        [
            str(conversation_id),
            str(message.get("local_id") or ""),
            str(message.get("sort_seq") or ""),
            str(message.get("create_time") or ""),
            str(message.get("sender_id") or message.get("sender_username") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
