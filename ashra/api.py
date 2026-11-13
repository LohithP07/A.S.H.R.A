"""FastAPI application for A.S.H.R.A.

The API keeps the original viewer contract and adds the Phase 1 foundation:

    GET  /health          liveness + version
    GET  /status          configuration, vault, session, and tool summary
    POST /chat            complete answer (existing contract)
    POST /chat/stream     server-sent events for the same answer
    POST /remember        create a capture
    POST /append          append to a capture
    POST /rename          rename a capture
    POST /forget          delete a capture (confirmation required)
    GET  /sessions/{id}   persisted conversation history
    GET  /tools           tools the caller may use
    POST /tools/invoke    invoke a tool through the permission gate
    GET  /agents          specialist subagents and their scopes
    GET  /integrations    connected MCP servers and their tools
    GET  /approvals       pending and recent approval requests
    POST /approvals/{id}  approve or deny a pending tool call
    GET  /audit           recent audit events
    POST /rebuild         rebuild the viewer graph

Every request gets an ``X-Request-ID``.  Chat, vault, and tool actions are
written to the SQLite audit log.  API keys are never returned by any endpoint.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import build
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

from .agents import (
    SPAWN_TOOL_NAME,
    AgentRegistry,
    SubagentRunner,
    build_default_agents,
    build_spawn_tool,
    tool_context,
)
from .chat import ChatService
from .config import load_config
from .db import Database, SQLiteSessionStore
from .errors import (
    ApprovalRequired,
    ConfigError,
    NotConfiguredError,
    PermissionDenied,
    ProviderError,
    ToolInputError,
    ToolNotFound,
)
from .integrations import IntegrationManager, load_integrations
from .memory import MemoryStore
from .notes import load_notes
from .orchestrator import Orchestrator
from .remember import append as append_capture
from .remember import forget as forget_capture
from .remember import remember as remember_capture
from .remember import rename as rename_capture
from .sessions import SESSION_RE
from .tools import Permission, ToolRegistry, build_default_registry, parse_permission

VERSION = "0.7.1"
MAX_BODY_BYTES = 64 * 1024
BLOCKED_NAMES = {"config.json", ".env", ".git"}

logger = logging.getLogger("ashra.api")


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    question: str = ""
    session_id: str | None = None
    permission: str = "read"


class RememberRequest(BaseModel):
    text: str = ""
    source: str = "typed"


class CaptureTarget(BaseModel):
    path: str | None = None
    last: bool = False


class AppendRequest(CaptureTarget):
    text: str = ""


class RenameRequest(CaptureTarget):
    title: str = ""


class ForgetRequest(CaptureTarget):
    confirm: bool = False


class ToolInvokeRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    permission: str = "read"
    approved: bool = False


class ApprovalDecision(BaseModel):
    approve: bool = False


class MemoryCreate(BaseModel):
    content: str = ""
    kind: str = "fact"
    status: str = "proposed"
    source: str = "manual"
    source_ref: str | None = None
    confidence: float | None = None
    sensitivity: str | None = None
    entities: list[str] = Field(default_factory=list)
    pinned: bool = False


class MemoryPropose(BaseModel):
    text: str = ""
    source: str = "manual"
    source_ref: str | None = None


class MemoryRemember(BaseModel):
    text: str = ""
    source: str = "manual"
    source_ref: str | None = None


class MemoryUpdate(BaseModel):
    content: str | None = None
    kind: str | None = None
    pinned: bool | None = None
    confidence: float | None = None
    sensitivity: str | None = None
    entities: list[str] | None = None


class MemoryForget(BaseModel):
    confirm: bool = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


def _sse(event: dict[str, Any]) -> str:
    name = str(event.get("event", "message"))
    return f"event: {name}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _error_code(exc: Exception) -> str:
    if isinstance(exc, NotConfiguredError):
        return "model_not_configured"
    if isinstance(exc, ConfigError):
        return "config_error"
    if isinstance(exc, ProviderError):
        return "provider_error"
    if isinstance(exc, ToolNotFound):
        return "tool_not_found"
    if isinstance(exc, PermissionDenied):
        return "permission_denied"
    if isinstance(exc, ApprovalRequired):
        return "approval_required"
    if isinstance(exc, (ToolInputError, ValueError)):
        return "bad_request"
    return "server_error"


def _capture_payload(
    result: Any,
    memories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "title": result.title,
        "path": result.path,
        "node": result.node,
        "related": result.related,
        "reply": result.reply,
        "created": result.created,
        "source": result.source,
        "memories": memories or [],
        "graph": result.graph,
    }


def _tool_audit(database: Database):
    def record(payload: dict[str, Any]) -> None:
        database.record_audit(
            "tool",
            str(payload.get("tool", "unknown")),
            status=str(payload.get("status", "ok")),
            permission=payload.get("permission"),
            request_id=payload.get("request_id"),
            detail=payload,
        )

    return record


def _agent_audit(database: Database):
    def record(payload: dict[str, Any]) -> None:
        database.record_audit(
            "agent",
            str(payload.get("agent", "unknown")),
            status=str(payload.get("status", "ok")),
            permission=payload.get("permission"),
            request_id=payload.get("request_id"),
            detail=payload,
        )

    return record


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------
def create_app(
    base_dir: Path | None = None,
    *,
    chat: ChatService | None = None,
    database: Database | None = None,
    registry: ToolRegistry | None = None,
    orchestrator: Orchestrator | None = None,
    integrations: IntegrationManager | None = None,
    agents: AgentRegistry | None = None,
    memory: MemoryStore | None = None,
    viewer_dir: Path | None = None,
) -> FastAPI:
    base = Path(base_dir or Path(__file__).resolve().parent.parent)
    viewer = Path(viewer_dir or base / "viewer")
    db = database or Database(base / "data" / "ashra.db")
    sessions = SQLiteSessionStore(db)
    memory_store = memory or MemoryStore(db)
    service = chat or ChatService(base, sessions=sessions, memory=memory_store)
    tools = registry or build_default_registry(
        base,
        audit=_tool_audit(db),
        memory=memory_store,
    )

    agent_registry = agents or AgentRegistry(build_default_agents())
    subagents: SubagentRunner | None = None
    if isinstance(service, ChatService):
        subagents = SubagentRunner(
            tools,
            lambda: service.provider_factory(load_config(service.config_path)),
            audit=_agent_audit(db),
        )
        try:
            tools.get(SPAWN_TOOL_NAME)
        except ToolNotFound:
            tools.register(build_spawn_tool(subagents, agent_registry))

    agent = orchestrator
    if agent is None and isinstance(service, ChatService):
        agent = Orchestrator(service, tools, db)

    integration_manager = integrations
    if integration_manager is None:
        configs = load_integrations(base / "config.json")
        if configs:
            integration_manager = IntegrationManager(configs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.initialize()
        if integration_manager is not None:
            # Probing an MCP server can block; keep it off the event loop.
            await run_in_threadpool(integration_manager.connect_all)
            integration_manager.register_tools(tools)
        try:
            yield
        finally:
            if integration_manager is not None:
                integration_manager.close()

    app = FastAPI(
        title="A.S.H.R.A",
        version=VERSION,
        description="Local-first, note-grounded personal assistant.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.base_dir = base
    app.state.database = db
    app.state.chat = service
    app.state.tools = tools
    app.state.orchestrator = agent
    app.state.integrations = integration_manager
    app.state.agents = agent_registry
    app.state.subagents = subagents
    app.state.memory = memory_store

    # Compress large static assets and JSON.  Event streams are excluded by
    # Starlette's default content-type filter, so SSE still flushes per token.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ------------------------------------------------------------------
    # middleware
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        path = unquote(request.url.path)
        parts = [part for part in path.split("/") if part]
        if any(part.lower() in BLOCKED_NAMES for part in parts):
            return _error(404, "not_found", "Not found.")

        length_header = request.headers.get("content-length")
        if length_header:
            try:
                length = int(length_header)
            except ValueError:
                return _error(400, "bad_request", "Invalid Content-Length header.")
            if length > MAX_BODY_BYTES:
                return _error(413, "payload_too_large", "Payload is too large.")

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        # Static viewer assets may be cached; API responses may not.  Vendor
        # libraries are pinned by a version query string, so they are safe to
        # keep for a year.  HTML/CSS/JS revalidate, which is a cheap 304.
        if path.startswith("/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path == "/" or path.endswith((".html", ".css", ".js", ".svg", ".png")):
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        logger.info("%s %s -> %s [%s]", request.method, path, response.status_code, request_id)
        return response

    # ------------------------------------------------------------------
    # exception mapping
    # ------------------------------------------------------------------
    @app.exception_handler(NotConfiguredError)
    async def not_configured(_request: Request, exc: NotConfiguredError):
        return _error(503, "model_not_configured", str(exc))

    @app.exception_handler(ConfigError)
    async def config_error(_request: Request, exc: ConfigError):
        return _error(500, "config_error", str(exc))

    @app.exception_handler(ProviderError)
    async def provider_error(_request: Request, exc: ProviderError):
        return _error(502, "provider_error", str(exc))

    @app.exception_handler(ToolNotFound)
    async def tool_not_found(_request: Request, exc: ToolNotFound):
        return _error(404, "tool_not_found", str(exc))

    @app.exception_handler(PermissionDenied)
    async def permission_denied(_request: Request, exc: PermissionDenied):
        return _error(403, "permission_denied", str(exc))

    @app.exception_handler(ApprovalRequired)
    async def approval_required(_request: Request, exc: ApprovalRequired):
        return _error(409, "approval_required", str(exc))

    @app.exception_handler(ToolInputError)
    async def tool_input_error(_request: Request, exc: ToolInputError):
        return _error(400, "bad_request", str(exc))

    @app.exception_handler(ValueError)
    async def value_error(_request: Request, exc: ValueError):
        return _error(400, "bad_request", str(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return _error(400, "bad_request", "Send valid JSON with the expected fields.")

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception):
        logger.error("unexpected error: %s\n%s", exc, traceback.format_exc())
        return _error(
            500,
            "server_error",
            "A.S.H.R.A hit an unexpected error. Check the server log.",
        )

    # ------------------------------------------------------------------
    # health / status
    # ------------------------------------------------------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "name": "A.S.H.R.A",
            "version": VERSION,
            "time": time.time(),
        }

    @app.get("/status")
    def status() -> dict[str, Any]:
        config = None
        config_error = None
        try:
            config = load_config(base / "config.json")
        except ConfigError as exc:
            config_error = str(exc)

        notes_dir = base / "notes"
        note_count = 0
        try:
            note_count = sum(1 for _ in notes_dir.rglob("*.md"))
        except OSError:
            pass

        memory_stats = memory_store.stats()
        return {
            "status": "ok",
            "name": "A.S.H.R.A",
            "version": VERSION,
            "configured": bool(config and config.configured),
            "provider": config.provider if config else None,
            "model": config.model if config else None,
            "config_error": config_error,
            "notes": note_count,
            "memories": memory_stats["total"],
            "memory_proposals": memory_stats["proposed"],
            "sessions": db.count_sessions(),
            "messages": db.count_messages(),
            "approvals": db.count_approvals(),
            "tools": len(tools.list(Permission.ADMIN)),
            "agents": len(agent_registry),
            "integrations": (
                integration_manager.status() if integration_manager is not None else []
            ),
            "database": str(db.path),
            "viewer": str(viewer),
        }

    @app.get("/home")
    def home() -> dict[str, Any]:
        """One aggregate read for the Home hub: brief, inbox, and recents."""
        db.expire_approvals()
        stats = memory_store.stats()
        proposals = memory_store.list(status="proposed", limit=12)
        approvals = db.list_approvals(status="pending", limit=12)
        needs = len(proposals) + len(approvals)

        if needs:
            parts: list[str] = []
            if proposals:
                label = "memory proposal" if len(proposals) == 1 else "memory proposals"
                parts.append(f"{len(proposals)} {label}")
            if approvals:
                label = "pending action" if len(approvals) == 1 else "pending actions"
                parts.append(f"{len(approvals)} {label}")
            headline = (
                "One thing needs you."
                if needs == 1
                else f"{needs} things need you."
            )
            detail = "Review " + " and ".join(parts) + "."
        else:
            headline = "Nothing needs you right now."
            detail = (
                f"The brain holds {stats['confirmed']} confirmed "
                f"{'memory' if stats['confirmed'] == 1 else 'memories'} "
                f"across {len(stats['by_kind'])} "
                f"{'kind' if len(stats['by_kind']) == 1 else 'kinds'}. "
                "Capture something new or ask the Galaxy a question."
            )

        return {
            "status": status(),
            "brief": {
                "headline": headline,
                "detail": detail,
                "needs": needs,
                "generated_at": time.time(),
            },
            "needs_you": {
                "memories": proposals,
                "approvals": approvals,
                "count": needs,
            },
            "recent": {
                "memories": memory_store.timeline(status="confirmed", limit=8),
                "sessions": db.recent_sessions(limit=8),
                "audit": [
                    {key: value for key, value in event.items() if key != "detail"}
                    for event in db.list_audit(limit=10)
                ],
            },
            "stats": stats,
        }

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------
    @app.post("/chat")
    def chat_endpoint(request_body: ChatRequest, request: Request) -> dict[str, Any]:
        request_id = _request_id(request)
        granted = parse_permission(request_body.permission)
        try:
            if agent is not None:
                result = agent.ask(
                    request_body.question,
                    session_id=request_body.session_id,
                    granted=granted,
                    request_id=request_id,
                )
                payload = result.as_dict()
            else:
                plain = service.ask(
                    request_body.question,
                    session_id=request_body.session_id,
                )
                payload = {
                    "status": "ok",
                    "answer": plain.answer,
                    "nodes": plain.nodes,
                    "sources": plain.sources,
                    "session_id": plain.session_id,
                    "model": plain.model,
                    "memories_proposed": [
                        item.get("id") for item in plain.proposals
                    ],
                }
        except Exception as exc:
            db.record_audit(
                "chat",
                "ask",
                status="error",
                permission=granted.value,
                request_id=request_id,
                session_id=request_body.session_id,
                detail={
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                },
            )
            raise

        db.record_audit(
            "chat",
            "ask",
            status=str(payload.get("status") or "ok"),
            permission=granted.value,
            request_id=request_id,
            session_id=payload.get("session_id"),
            detail={
                "nodes": payload.get("nodes", []),
                "model": payload.get("model"),
                "chars": len(str(payload.get("answer") or "")),
                "tools": [item.get("name") for item in payload.get("tools", [])],
                "memories_proposed": payload.get("memories_proposed", []),
            },
        )
        return payload

    @app.post("/chat/stream")
    async def chat_stream(request_body: ChatRequest, request: Request):
        request_id = _request_id(request)
        granted = parse_permission(request_body.permission)

        async def events():
            status = "ok"
            detail: dict[str, Any] = {}
            session_id = request_body.session_id
            if agent is not None:
                stream = agent.stream(
                    request_body.question,
                    session_id=request_body.session_id,
                    granted=granted,
                    request_id=request_id,
                )
            else:
                stream = service.stream(
                    request_body.question,
                    session_id=request_body.session_id,
                )
            try:
                async for event in iterate_in_threadpool(stream):
                    if await request.is_disconnected():
                        break
                    if event.get("event") in {"start", "done"} and event.get("session_id"):
                        session_id = str(event["session_id"])
                    if event.get("event") == "done":
                        status = str(event.get("status") or "ok")
                        detail = {
                            "nodes": event.get("nodes", []),
                            "model": event.get("model"),
                            "chars": len(str(event.get("answer", ""))),
                            "tools": [
                                item.get("name")
                                for item in event.get("tools", [])
                                if isinstance(item, dict)
                            ],
                            "memories_proposed": event.get("memories_proposed", []),
                        }
                    yield _sse(event)
            except Exception as exc:
                status = "error"
                detail = {
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                }
                yield _sse(
                    {
                        "event": "error",
                        "error": _error_code(exc),
                        "detail": str(exc),
                    }
                )
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
                db.record_audit(
                    "chat",
                    "stream",
                    status=status,
                    permission=granted.value,
                    request_id=request_id,
                    session_id=session_id,
                    detail=detail,
                )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # vault actions
    # ------------------------------------------------------------------
    @app.post("/remember")
    def remember_endpoint(request_body: RememberRequest, request: Request):
        try:
            result = remember_capture(
                base,
                request_body.text,
                source=request_body.source,
            )
        except ValueError as exc:
            return _error(400, "bad_capture", str(exc))
        memories: list[dict[str, Any]] = []
        try:
            memories = memory_store.remember_explicit(
                request_body.text,
                source="capture",
                source_ref=f"notes/{result.path}",
            )
        except ValueError:
            # A credential-shaped capture still belongs in the vault, but it
            # must never become long-term memory.
            memories = []
        db.record_audit(
            "vault",
            "remember",
            request_id=_request_id(request),
            detail={
                "path": result.path,
                "title": result.title,
                "memories": [item.get("id") for item in memories],
            },
        )
        return _capture_payload(result, memories)

    @app.post("/append")
    def append_endpoint(request_body: AppendRequest, request: Request):
        try:
            result = append_capture(
                base,
                request_body.text,
                path=request_body.path,
                last=request_body.last,
            )
        except ValueError as exc:
            return _error(400, "bad_capture", str(exc))
        db.record_audit(
            "vault",
            "append",
            request_id=_request_id(request),
            detail={"path": result.path, "title": result.title},
        )
        return _capture_payload(result)

    @app.post("/rename")
    def rename_endpoint(request_body: RenameRequest, request: Request):
        try:
            result = rename_capture(
                base,
                request_body.title,
                path=request_body.path,
                last=request_body.last,
            )
        except ValueError as exc:
            return _error(400, "bad_capture", str(exc))
        db.record_audit(
            "vault",
            "rename",
            request_id=_request_id(request),
            detail={"path": result.path, "title": result.title},
        )
        return _capture_payload(result)

    @app.post("/forget")
    def forget_endpoint(request_body: ForgetRequest, request: Request):
        if request_body.confirm is not True:
            return _error(
                400,
                "confirmation_required",
                "Deleting a capture needs confirmation.",
            )
        try:
            result = forget_capture(
                base,
                path=request_body.path,
                last=request_body.last,
            )
        except ValueError as exc:
            return _error(400, "bad_capture", str(exc))
        db.record_audit(
            "vault",
            "forget",
            request_id=_request_id(request),
            detail={"path": result.path, "title": result.title},
        )
        return _capture_payload(result)

    # ------------------------------------------------------------------
    # memory
    # ------------------------------------------------------------------
    @app.get("/memory")
    def list_memory(
        status: str | None = None,
        kind: str | None = None,
        pinned: bool | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return {
            "memories": memory_store.list(
                status=status,
                kind=kind,
                pinned=pinned,
                limit=limit,
            ),
            "stats": memory_store.stats(),
        }

    @app.get("/memory/stats")
    def memory_stats() -> dict[str, Any]:
        return memory_store.stats()

    @app.get("/memory/proposals")
    def memory_proposals(limit: int = 100) -> dict[str, Any]:
        return {
            "memories": memory_store.list(status="proposed", limit=limit),
            "stats": memory_store.stats(),
        }

    @app.get("/memory/search")
    def memory_search(
        q: str = "",
        limit: int = 12,
        status: str | None = "confirmed",
        kind: str | None = None,
    ) -> dict[str, Any]:
        return {
            "query": q,
            "memories": memory_store.search(
                q,
                limit=limit,
                status=status,
                kind=kind,
            ),
        }

    @app.get("/memory/graph")
    def memory_graph(status: str | None = None, limit: int = 500) -> dict[str, Any]:
        return memory_store.graph(status=status, limit=limit)

    @app.get("/memory/timeline")
    def memory_timeline(status: str | None = None, limit: int = 120) -> dict[str, Any]:
        return {"memories": memory_store.timeline(status=status, limit=limit)}

    @app.post("/memory")
    def create_memory(body: MemoryCreate, request: Request) -> Any:
        try:
            item = memory_store.add(
                body.content,
                kind=body.kind,
                status=body.status,
                source=body.source,
                source_ref=body.source_ref,
                confidence=body.confidence,
                sensitivity=body.sensitivity,
                entities=body.entities or None,
                pinned=body.pinned,
            )
        except ValueError as exc:
            return _error(400, "bad_memory", str(exc))
        db.record_audit(
            "memory",
            "create",
            status=item.get("status", "proposed"),
            request_id=_request_id(request),
            detail={"memory_id": item.get("id"), "kind": item.get("kind")},
        )
        return {"memory": item}

    @app.post("/memory/propose")
    def propose_memory(body: MemoryPropose, request: Request) -> Any:
        try:
            items = memory_store.propose_from_text(
                body.text,
                source=body.source,
                source_ref=body.source_ref,
            )
        except ValueError as exc:
            return _error(400, "bad_memory", str(exc))
        db.record_audit(
            "memory",
            "propose",
            request_id=_request_id(request),
            detail={"count": len(items), "ids": [item.get("id") for item in items]},
        )
        return {"memories": items, "count": len(items)}

    @app.post("/memory/remember")
    def remember_memory(body: MemoryRemember, request: Request) -> Any:
        try:
            items = memory_store.remember_explicit(
                body.text,
                source=body.source,
                source_ref=body.source_ref,
            )
        except ValueError as exc:
            return _error(400, "bad_memory", str(exc))
        db.record_audit(
            "memory",
            "remember",
            request_id=_request_id(request),
            detail={"count": len(items), "ids": [item.get("id") for item in items]},
        )
        return {"memories": items, "count": len(items)}

    @app.post("/memory/ingest")
    def ingest_memory(request: Request) -> dict[str, Any]:
        result = memory_store.ingest_notes(load_notes())
        db.record_audit(
            "memory",
            "ingest",
            request_id=_request_id(request),
            detail=result,
        )
        return {"ok": True, **result}

    @app.post("/memory/consolidate")
    def consolidate_memory(request: Request) -> dict[str, Any]:
        result = memory_store.consolidate()
        db.record_audit(
            "memory",
            "consolidate",
            request_id=_request_id(request),
            detail=result,
        )
        return {"ok": True, **result}

    @app.get("/memory/{memory_id}")
    def get_memory(memory_id: str) -> Any:
        item = memory_store.get(memory_id)
        if item is None:
            return _error(404, "not_found", "No such memory.")
        return {"memory": item, "related": memory_store.related(memory_id)}

    @app.post("/memory/{memory_id}/confirm")
    def confirm_memory(memory_id: str, request: Request) -> Any:
        item = memory_store.confirm(memory_id)
        if item is None:
            return _error(404, "not_found", "No such memory.")
        db.record_audit(
            "memory",
            "confirm",
            request_id=_request_id(request),
            detail={"memory_id": memory_id},
        )
        return {"memory": item}

    @app.post("/memory/{memory_id}/reject")
    def reject_memory(memory_id: str, request: Request) -> Any:
        item = memory_store.reject(memory_id)
        if item is None:
            return _error(404, "not_found", "No such memory.")
        db.record_audit(
            "memory",
            "reject",
            request_id=_request_id(request),
            detail={"memory_id": memory_id},
        )
        return {"memory": item}

    @app.patch("/memory/{memory_id}")
    def update_memory(memory_id: str, body: MemoryUpdate, request: Request) -> Any:
        changes = body.model_dump(exclude_unset=True)
        try:
            item = memory_store.update(memory_id, **changes)
        except ValueError as exc:
            return _error(400, "bad_memory", str(exc))
        if item is None:
            return _error(404, "not_found", "No such memory.")
        db.record_audit(
            "memory",
            "update",
            request_id=_request_id(request),
            detail={"memory_id": memory_id, "fields": sorted(changes)},
        )
        return {"memory": item}

    @app.post("/memory/{memory_id}/forget")
    def forget_memory(
        memory_id: str,
        body: MemoryForget,
        request: Request,
    ) -> Any:
        if body.confirm is not True:
            return _error(
                400,
                "confirmation_required",
                "Forgetting a memory needs confirmation.",
            )
        removed = memory_store.forget(memory_id)
        if not removed:
            return _error(404, "not_found", "No such memory.")
        db.record_audit(
            "memory",
            "forget",
            request_id=_request_id(request),
            detail={"memory_id": memory_id},
        )
        return {"ok": True, "forgotten": memory_id}

    # ------------------------------------------------------------------
    # sessions, tools, audit
    # ------------------------------------------------------------------
    @app.get("/sessions/{session_id}")
    def session_history(session_id: str):
        if not SESSION_RE.match(session_id):
            return _error(400, "bad_request", "That is not a valid session id.")
        return {
            "session_id": session_id,
            "messages": db.history(session_id),
        }

    @app.get("/tools")
    def list_tools(permission: str = "read") -> dict[str, Any]:
        granted = parse_permission(permission)
        return {
            "permission": granted.value,
            "tools": tools.list(granted),
        }

    @app.post("/tools/invoke")
    def invoke_tool(request_body: ToolInvokeRequest, request: Request) -> dict[str, Any]:
        granted = parse_permission(request_body.permission)
        request_id = _request_id(request)
        try:
            with tool_context(granted, request_id):
                result = tools.invoke(
                    request_body.name,
                    request_body.arguments,
                    granted=granted,
                    approved=request_body.approved,
                    request_id=request_id,
                )
        except (ToolNotFound, PermissionDenied, ApprovalRequired, ToolInputError) as exc:
            db.record_audit(
                "tool",
                request_body.name,
                status="error",
                permission=granted.value,
                request_id=request_id,
                detail={
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                },
            )
            raise
        return {
            "ok": True,
            "tool": request_body.name,
            "permission": granted.value,
            "result": result,
        }

    @app.get("/agents")
    def list_agents() -> dict[str, Any]:
        return {"agents": agent_registry.list()}

    @app.get("/agents/{name}")
    def get_agent(name: str) -> Any:
        try:
            spec = agent_registry.get(name)
        except ToolNotFound:
            return _error(404, "not_found", "No such agent.")
        return {"agent": spec.as_dict()}

    @app.get("/integrations")
    def list_integrations() -> dict[str, Any]:
        return {
            "integrations": (
                integration_manager.status() if integration_manager is not None else []
            )
        }

    @app.get("/integrations/{name}")
    def get_integration(name: str) -> Any:
        if integration_manager is None:
            return _error(404, "not_found", "No such integration.")
        for item in integration_manager.status():
            if item["name"] == str(name).strip().lower():
                return {"integration": item}
        return _error(404, "not_found", "No such integration.")

    @app.get("/approvals")
    def list_approvals(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"approvals": db.list_approvals(status=status, limit=limit)}

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str) -> Any:
        row = db.get_approval(approval_id)
        if row is None:
            return _error(404, "not_found", "No such approval.")
        return {"approval": row}

    @app.post("/approvals/{approval_id}")
    def decide_approval(
        approval_id: str,
        request_body: ApprovalDecision,
        request: Request,
    ) -> Any:
        if agent is None:
            return _error(503, "not_available", "The orchestrator is not available.")
        try:
            result = agent.resume_result(
                approval_id,
                approve=request_body.approve,
                request_id=_request_id(request),
            )
        except ValueError as exc:
            return _error(400, "bad_request", str(exc))
        return result.as_dict()

    @app.get("/audit")
    def audit_log(limit: int = 50, event: str | None = None) -> dict[str, Any]:
        return {"events": db.list_audit(limit=limit, event=event)}

    @app.post("/rebuild")
    def rebuild(request: Request) -> dict[str, Any]:
        notes = build.discover_notes()
        links = build.build_graph(notes)
        build.write_output(notes, links)
        payload = build.graph_payload(notes, links)
        db.record_audit(
            "vault",
            "rebuild",
            request_id=_request_id(request),
            detail={"notes": len(notes), "links": len(links)},
        )
        return {
            "ok": True,
            "notes": len(notes),
            "links": len(links),
            "graph": payload,
        }

    # ------------------------------------------------------------------
    # static viewer (mounted last so API routes win)
    # ------------------------------------------------------------------
    if viewer.is_dir():
        app.mount("/", StaticFiles(directory=str(viewer), html=True), name="viewer")

    return app
