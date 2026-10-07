from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    wechat_cache_dir: Path
    compatible_versions: frozenset[str]
    host: str = "127.0.0.1"
    port: int = 18765
    incremental_lookback_seconds: int = 24 * 60 * 60
    # Decrypted copies contain every conversation in plaintext, not only the
    # selected ones, so they are removed after each sync unless explicitly kept.
    keep_decrypted_cache: bool = False

    @classmethod
    def load(cls) -> "Settings":
        default_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ChatTrace"
        data_dir = Path(os.environ.get("CHATTRACE_DATA_DIR", default_root)).resolve()
        versions = os.environ.get("CHATTRACE_COMPATIBLE_VERSIONS", "4.1.15.13")
        compatible = frozenset(v.strip() for v in versions.split(",") if v.strip())
        return cls(
            data_dir=data_dir,
            db_path=data_dir / "chattrace.db",
            wechat_cache_dir=data_dir / "wechat-cache",
            compatible_versions=compatible,
            port=int(os.environ.get("CHATTRACE_PORT", "18765")),
            keep_decrypted_cache=_env_flag("CHATTRACE_KEEP_DECRYPTED_CACHE"),
        )

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def decrypted_dir(self) -> Path:
        return self.wechat_cache_dir / "decrypted"

    @property
    def keys_file(self) -> Path:
        return self.wechat_cache_dir / "keys.json"

    @property
    def allowed_hosts(self) -> list[str]:
        return ["127.0.0.1", "localhost"]

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://{host}:{self.port}" for host in self.allowed_hosts}

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.wechat_cache_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
