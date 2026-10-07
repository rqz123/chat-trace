from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .config import Settings
from .orchestrator import ArchiveOrchestrator, Environment, QUIET_SECONDS_RANGE
from .storage import Storage
from .summarizer import TopicSummarizer
from .sync_service import SyncService
from .wechat_adapter import WeChatReader, remove_decrypted_cache, seal_key_file


logger = logging.getLogger("chattrace")
static_dir = Path(__file__).with_name("static")


class ConversationConfig(BaseModel):
    enabled: bool
    history_mode: str = Field(default="all", pattern="^(all|since|recent_30d|recent_90d)$")
    history_start_at: int | None = None

    @model_validator(mode="after")
    def _check_start(self) -> "ConversationConfig":
        if self.history_mode == "since":
            if not self.history_start_at or self.history_start_at <= 0:
                raise ValueError("指定开始日期时必须提供 history_start_at")
        else:
            self.history_start_at = None
        return self


class OrchestrationRequest(BaseModel):
    quiet_seconds: int = Field(default=45, ge=QUIET_SECONDS_RANGE[0], le=QUIET_SECONDS_RANGE[1])


class SummaryRangeRequest(BaseModel):
    conversation_id: int
    start_at: int
    end_at: int
    range_label: str = ""


class AISettingsRequest(BaseModel):
    model_tier: str


class HideConversationsRequest(BaseModel):
    conversation_ids: list[int] = Field(min_length=1, max_length=5000)
    hidden: bool = True


class HideInactiveRequest(BaseModel):
    days: int = Field(ge=7, le=3650)


def configure_logging(settings: Settings) -> None:
    """Write operational logs to ``<data_dir>/logs``.

    Code must never log message bodies, contact identifiers or keys; log local
    numeric ids and counts instead.
    """
    root = logging.getLogger("chattrace")
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    handler = RotatingFileHandler(
        settings.logs_dir / "chattrace.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def create_app(
    settings: Settings | None = None,
    *,
    sync_service: SyncService | None = None,
    reader_factory=None,
    orchestrator_env: Environment | None = None,
    llm_factory=None,
) -> FastAPI:
    settings = settings or Settings.load()
    reader_factory = reader_factory or (lambda: WeChatReader(settings))
    storage = sync_service.storage if sync_service else Storage(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.ensure_directories()
        storage.initialize()
        interrupted = storage.recover_interrupted_jobs()
        if interrupted:
            logger.warning("已将 %d 个上次未结束的同步任务标记为中断", interrupted)
        if storage.recover_interrupted_summaries():
            logger.warning("已将上次未结束的话题总结标记为中断")
        if seal_key_file(settings.keys_file):
            logger.info("已将明文密钥缓存转换为 DPAPI 加密存储")
        if not settings.keep_decrypted_cache:
            removed, _ = remove_decrypted_cache(settings)
            if removed:
                logger.info("已清理 %d 个遗留的微信解密副本", removed)
        logger.info("聊迹 %s 已启动，搜索模式: %s", __version__, "FTS5" if storage.fts_enabled else "LIKE")
        yield

    app = FastAPI(title="聊迹 ChatTrace", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage
    app.state.sync_service = sync_service or SyncService(
        storage, settings, reader_factory=reader_factory
    )
    app.state.orchestrator = ArchiveOrchestrator(
        storage, settings, app.state.sync_service, environment=orchestrator_env
    )
    app.state.summarizer = TopicSummarizer(storage, settings, llm_factory=llm_factory)

    # Reject requests whose Host is not loopback (DNS rebinding) and any
    # browser request initiated by another origin (CSRF from web pages).
    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if (origin and origin not in settings.allowed_origins) or fetch_site in {
            "cross-site",
            "same-site",
        }:
            return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Revalidate every load (ETag keeps it cheap) so an updated UI is
            # never hidden behind a heuristically cached old copy.
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/status")
    def status():
        reader_status = reader_factory().status().to_dict()
        reader_status.update(
            {
                "app_version": __version__,
                "data_dir": str(settings.data_dir),
                "compatible_versions": sorted(settings.compatible_versions),
                "selected_conversations": len(storage.selected_conversations()),
                "search_mode": "fts5" if storage.fts_enabled else "like",
            }
        )
        return reader_status

    @app.post("/api/conversations/scan")
    def scan_conversations():
        reader = reader_factory()
        try:
            sessions = reader.list_sessions()
            count = storage.upsert_conversations(sessions)
            return {"scanned": count}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            # Scanning decrypts session/contact databases; do not leave them.
            # Skip while a sync job is reading the same copies.
            if storage.active_job_id() is None and hasattr(reader, "close"):
                reader.close()

    @app.get("/api/conversations")
    def conversations():
        return storage.list_conversations()

    @app.put("/api/conversations/{conversation_id}")
    def configure_conversation(conversation_id: int, body: ConversationConfig):
        row = storage.configure_conversation(
            conversation_id,
            enabled=body.enabled,
            history_mode=body.history_mode,
            history_start_at=body.history_start_at,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return row

    @app.post("/api/conversations/hide")
    def hide_conversations(body: HideConversationsRequest):
        changed = storage.set_conversations_hidden(body.conversation_ids, body.hidden)
        return {"changed": changed}

    @app.get("/api/conversations/inactive")
    def inactive_conversations(days: int = Query(ge=7, le=3650)):
        return {"count": storage.count_inactive_conversations(int(time.time()) - days * 86400)}

    @app.post("/api/conversations/hide-inactive")
    def hide_inactive(body: HideInactiveRequest):
        return {"hidden": storage.hide_inactive_conversations(int(time.time()) - body.days * 86400)}

    @app.post("/api/sync")
    def start_sync():
        try:
            return {"job_id": app.state.sync_service.start()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sync/{job_id}/cancel")
    def cancel_sync(job_id: int):
        if not app.state.sync_service.cancel(job_id):
            raise HTTPException(status_code=404, detail="该任务未在运行")
        return {"cancelling": job_id}

    @app.get("/api/orchestration")
    def orchestration_state():
        return app.state.orchestrator.snapshot()

    @app.post("/api/orchestration/start")
    def orchestration_start(body: OrchestrationRequest):
        try:
            return app.state.orchestrator.start(body.quiet_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orchestration/cancel")
    def orchestration_cancel():
        if not app.state.orchestrator.cancel():
            raise HTTPException(status_code=404, detail="没有正在进行的启动并归档流程")
        return app.state.orchestrator.snapshot()

    @app.get("/api/jobs")
    def jobs(limit: int = Query(20, ge=1, le=100)):
        return storage.list_jobs(limit)

    @app.get("/api/messages")
    def messages(
        conversation_id: list[int] | None = Query(default=None),
        start_at: int | None = None,
        end_at: int | None = None,
        q: str | None = None,
        order: str = Query("desc", pattern="^(asc|desc)$"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        total, rows = storage.query_messages(
            conversation_ids=conversation_id,
            start_at=start_at,
            end_at=end_at,
            query=q,
            order=order,
            limit=limit,
            offset=offset,
        )
        return {"total": total, "items": rows}

    summarizer: TopicSummarizer = app.state.summarizer

    @app.get("/api/ai/settings")
    def ai_settings():
        return summarizer.ai_settings().public()

    @app.put("/api/ai/settings")
    def update_ai_settings(body: AISettingsRequest):
        try:
            return summarizer.set_model_tier(body.model_tier).public()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/summaries/preview")
    def summary_preview(body: SummaryRangeRequest):
        try:
            return summarizer.preview(body.conversation_id, body.start_at, body.end_at)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/summaries")
    def create_summary(body: SummaryRangeRequest):
        try:
            summary_id = summarizer.start(
                body.conversation_id, body.start_at, body.end_at, body.range_label
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return storage.get_topic_summary(summary_id)

    @app.get("/api/summaries")
    def list_summaries(conversation_id: int | None = None, limit: int = Query(200, ge=1, le=500)):
        return storage.list_topic_summaries(conversation_id, limit)

    @app.get("/api/summaries/{summary_id}")
    def get_summary(summary_id: int):
        summary = storage.get_topic_summary(summary_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="总结不存在")
        return summary

    @app.post("/api/summaries/{summary_id}/cancel")
    def cancel_summary(summary_id: int):
        if not summarizer.cancel(summary_id):
            raise HTTPException(status_code=404, detail="该总结未在进行")
        return {"cancelling": summary_id}

    @app.delete("/api/summaries/{summary_id}")
    def delete_summary(summary_id: int):
        summarizer.cancel(summary_id)
        if not storage.delete_topic_summary(summary_id):
            raise HTTPException(status_code=404, detail="总结不存在")
        return {"deleted": summary_id}

    @app.get("/api/summary-topics/{topic_id}/messages")
    def topic_messages(topic_id: int):
        return storage.topic_source_messages(topic_id)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    return app


def run() -> None:
    settings = Settings.load()
    settings.ensure_directories()
    configure_logging(settings)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
