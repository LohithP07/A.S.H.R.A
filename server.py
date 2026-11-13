#!/usr/bin/env python3
"""
server.py - serve the viewer and expose the note-grounded /chat endpoint.

The browser only ever sees files inside ./viewer.  config.json stays in the
project root and is never served.

Standard library only.  Run:  python server.py
Stop with Ctrl+C.
"""

from __future__ import annotations

import json
import sys
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ashra.chat import ChatService
from ashra.config import load_config
from ashra.errors import ConfigError, NotConfiguredError, ProviderError
from ashra.remember import append as append_note
from ashra.remember import forget as forget_note
from ashra.remember import remember as capture_note
from ashra.remember import rename as rename_note

BASE_DIR = Path(__file__).resolve().parent
VIEWER_DIR = BASE_DIR / "viewer"
CONFIG_PATH = BASE_DIR / "config.json"
HOST = "127.0.0.1"
PORT = 4700
MAX_BODY_BYTES = 64 * 1024

# Defense in depth.  These are outside viewer/ already; refusing them here
# prevents a future accidental copy from becoming a browser-readable secret.
BLOCKED_NAMES = {"config.json", ".env", ".git"}

CHAT = ChatService(BASE_DIR)
_INVALID = object()


class ViewerHandler(SimpleHTTPRequestHandler):
    """Static viewer + POST /chat, rooted at viewer/."""

    server_version = "A.S.H.R.A/2.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEWER_DIR), **kwargs)

    def end_headers(self) -> None:
        # Always pick up a fresh build while developing the notes.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write(f"[viewer] {self.address_string()} - {fmt % args}\n")
        sys.stdout.flush()

    def _request_path(self) -> str:
        return unquote(urlsplit(self.path).path)

    def _is_blocked(self, path: str) -> bool:
        parts = [part for part in path.split("/") if part]
        return any(part.lower() in BLOCKED_NAMES for part in parts)

    def do_GET(self) -> None:
        if self._is_blocked(self._request_path()):
            self.send_error(404, "Not found")
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = self._request_path()
        routes = {
            "/chat": self._handle_chat,
            "/remember": self._handle_remember,
            "/forget": self._handle_forget,
            "/rename": self._handle_rename,
            "/append": self._handle_append,
        }
        if path not in routes:
            self.send_json(404, {"error": "Not found"})
            return

        payload = self._read_json()
        if payload is _INVALID:
            return

        routes[path](payload)

    def _capture_target(self, payload: dict) -> tuple[str | None, bool] | None:
        """Read the shared {path?, last?} selector used by capture actions."""
        last = payload.get("last") is True
        path = payload.get("path")
        if path is not None and not isinstance(path, str):
            self.send_json(400, {"error": "path must be a string."})
            return None
        if not last and not path:
            self.send_json(
                400,
                {"error": "bad_request", "detail": "Name a capture, or ask for the last one."},
            )
            return None
        return path, last

    def _send_capture(self, result) -> None:
        self.send_json(
            200,
            {
                "ok": True,
                "title": result.title,
                "path": result.path,
                "node": result.node,
                "related": result.related,
                "reply": result.reply,
                "created": result.created,
                "source": result.source,
                "graph": result.graph,
            },
        )

    def _read_json(self) -> dict | object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid Content-Length header."})
            return _INVALID

        if length <= 0:
            self.send_json(400, {"error": "Send a JSON body."})
            return _INVALID
        if length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "Payload is too large."})
            return _INVALID

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Send valid JSON."})
            return _INVALID

        if not isinstance(payload, dict):
            self.send_json(400, {"error": "The JSON body must be an object."})
            return _INVALID

        return payload

    def _handle_chat(self, payload: dict) -> None:
        question = payload.get("question", "")
        if not isinstance(question, str):
            self.send_json(400, {"error": "The question must be a string."})
            return

        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            self.send_json(400, {"error": "session_id must be a string."})
            return

        try:
            result = CHAT.ask(question, session_id=session_id)
        except NotConfiguredError as exc:
            self.send_json(
                503,
                {"error": "model_not_configured", "detail": str(exc)},
            )
            return
        except ConfigError as exc:
            self.send_json(500, {"error": "config_error", "detail": str(exc)})
            return
        except ProviderError as exc:
            self.send_json(502, {"error": "provider_error", "detail": str(exc)})
            return
        except ValueError as exc:
            self.send_json(400, {"error": "bad_request", "detail": str(exc)})
            return
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.send_json(
                500,
                {
                    "error": "server_error",
                    "detail": "A.S.H.R.A hit an unexpected error. Check the server log.",
                },
            )
            return

        self.send_json(
            200,
            {
                "answer": result.answer,
                "nodes": result.nodes,
                "sources": result.sources,
                "session_id": result.session_id,
                "model": result.model,
            },
        )

    def _handle_remember(self, payload: dict) -> None:
        text = payload.get("text", "")
        if not isinstance(text, str):
            self.send_json(400, {"error": "The text must be a string."})
            return

        source = payload.get("source", "typed")
        if not isinstance(source, str):
            self.send_json(400, {"error": "source must be a string."})
            return

        try:
            result = capture_note(BASE_DIR, text, source=source)
        except ValueError as exc:
            self.send_json(400, {"error": "bad_capture", "detail": str(exc)})
            return
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.send_json(
                500,
                {
                    "error": "server_error",
                    "detail": "The vault hit an unexpected error. Check the server log.",
                },
            )
            return

        self._send_capture(result)

    def _handle_forget(self, payload: dict) -> None:
        target = self._capture_target(payload)
        if target is None:
            return

        if payload.get("confirm") is not True:
            self.send_json(
                400,
                {
                    "error": "confirmation_required",
                    "detail": "Deleting a capture needs confirmation.",
                },
            )
            return

        try:
            result = forget_note(BASE_DIR, path=target[0], last=target[1])
        except ValueError as exc:
            self.send_json(400, {"error": "bad_capture", "detail": str(exc)})
            return
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.send_json(
                500,
                {
                    "error": "server_error",
                    "detail": "The vault hit an unexpected error. Check the server log.",
                },
            )
            return

        self._send_capture(result)

    def _handle_rename(self, payload: dict) -> None:
        target = self._capture_target(payload)
        if target is None:
            return

        title = payload.get("title", "")
        if not isinstance(title, str):
            self.send_json(400, {"error": "The title must be a string."})
            return

        try:
            result = rename_note(BASE_DIR, title, path=target[0], last=target[1])
        except ValueError as exc:
            self.send_json(400, {"error": "bad_capture", "detail": str(exc)})
            return
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.send_json(
                500,
                {
                    "error": "server_error",
                    "detail": "The vault hit an unexpected error. Check the server log.",
                },
            )
            return

        self._send_capture(result)

    def _handle_append(self, payload: dict) -> None:
        target = self._capture_target(payload)
        if target is None:
            return

        text = payload.get("text", "")
        if not isinstance(text, str):
            self.send_json(400, {"error": "The text must be a string."})
            return

        try:
            result = append_note(BASE_DIR, text, path=target[0], last=target[1])
        except ValueError as exc:
            self.send_json(400, {"error": "bad_capture", "detail": str(exc)})
            return
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.send_json(
                500,
                {
                    "error": "server_error",
                    "detail": "The vault hit an unexpected error. Check the server log.",
                },
            )
            return

        self._send_capture(result)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def assistant_status() -> str:
    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        return f"config error - {exc}"
    if not config.configured:
        return "not configured - paste your API key into config.json"
    return f"model {config.model}"


def main() -> int:
    if not VIEWER_DIR.is_dir():
        print(f"error: viewer folder not found: {VIEWER_DIR}", file=sys.stderr)
        return 1
    if not (VIEWER_DIR / "index.html").is_file():
        print("error: viewer/index.html is missing. Run python build.py first.", file=sys.stderr)
        return 1

    try:
        server = ThreadingHTTPServer((HOST, PORT), ViewerHandler)
    except OSError as exc:
        print(f"error: could not bind {HOST}:{PORT} - {exc}", file=sys.stderr)
        print("Is another server already using port 4700?", file=sys.stderr)
        return 1

    print(f"Serving {VIEWER_DIR}")
    print(f"Open in Chrome:  http://{HOST}:{PORT}/")
    print(f"A.S.H.R.A: {assistant_status()}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
