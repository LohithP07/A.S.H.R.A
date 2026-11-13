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
from starlette.concurrency import iterate_in_threadpool

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
from .orchestrator import Orchestrator
from .remember import append as append_capture
from .remember import forget as forget_capture
from .remember import remember as remember_capture
from .remember import rename as rename_capture
from .sessions import SESSION_RE
from .tools import Permission, ToolRegistry, build_default_registry, parse_permission

VERSION = "0.3.0"
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


def _capture_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "title": result.title,
        "path": result.path,
        "node": result.node,
        "related": result.related,
        "reply": result.reply,
        "created": result.created,
        "source": result.source,
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
    viewer_dir: Path | None = None,
) -> FastAPI:
    base = Path(base_dir or Path(__file__).resolve().parent.parent)
    viewer = Path(viewer_dir or base / "viewer")
    db = database or Database(base / "data" / "ashra.db")
    sessions = SQLiteSessionStore(db)
    service = chat or ChatService(base, sessions=sessions)
    tools = registry or build_default_registry(base, audit=_tool_audit(db))
    agent = orchestrator
    if agent is None and isinstance(service, ChatService):
        agent = Orchestrator(service, tools, db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.initialize()
        yield

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

        return {
            "status": "ok",
            "name": "A.S.H.R.A",
            "version": VERSION,
            "configured": bool(config and config.configured),
            "provider": config.provider if config else None,
            "model": config.model if config else None,
            "config_error": config_error,
            "notes": note_count,
            "sessions": db.count_sessions(),
            "messages": db.count_messages(),
            "approvals": db.count_approvals(),
            "tools": len(tools.list(Permission.ADMIN)),
            "database": str(db.path),
            "viewer": str(viewer),
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
        db.record_audit(
            "vault",
            "remember",
            request_id=_request_id(request),
            detail={"path": result.path, "title": result.title},
        )
        return _capture_payload(result)

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
