#!/usr/bin/env python3
"""server.py - Uvicorn entrypoint for the A.S.H.R.A API and viewer.

Run:  python server.py
Stop with Ctrl+C.

The FastAPI app lives in ``ashra/api.py``.  This file keeps the familiar
``python server.py`` command, the local-only bind address, and the startup
banner.  The browser only ever sees files inside ``viewer/``; ``config.json``
and the SQLite database stay in the project root and are never served.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import uvicorn

from ashra.api import MAX_BODY_BYTES, VERSION, create_app
from ashra.config import load_config
from ashra.errors import ConfigError

BASE_DIR = Path(__file__).resolve().parent
VIEWER_DIR = BASE_DIR / "viewer"
CONFIG_PATH = BASE_DIR / "config.json"
HOST = "127.0.0.1"
PORT = 4700
GRACEFUL_SHUTDOWN_SECONDS = 5

# Exposed for ``uvicorn server:app`` and for tests.
app = create_app(BASE_DIR)


def enable_console_ctrl_c() -> None:
    """Make Ctrl+C work when Python was started through an app alias.

    On Windows, ``python`` on PATH is often an app execution alias: a stub that
    launches the real interpreter as a child created with
    ``CREATE_NEW_PROCESS_GROUP``.  Children created that way start with Ctrl+C
    *disabled*, so the console event reaches the stub and never uvicorn — the
    server keeps serving after the window looks stopped.

    Passing NULL to ``SetConsoleCtrlHandler`` clears that flag and restores
    normal Ctrl+C processing for this process.  It is a no-op everywhere else,
    and on a directly launched interpreter, where Ctrl+C already works.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCtrlHandler(None, False)
    except Exception:  # pragma: no cover - a missing console is not fatal
        pass


def assistant_status() -> str:
    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        return f"config error - {exc}"
    if config.provider in {"ollama", "local"}:
        if not config.configured:
            return "not configured - set local.model in config.json, then start Ollama"
        return f"local model {config.for_local().model}"
    if not config.configured:
        return "not configured - store a key with: ashra-vault set ashra/cloud"
    return f"model {config.model}"


def main() -> int:
    if not VIEWER_DIR.is_dir():
        print(f"error: viewer folder not found: {VIEWER_DIR}", file=sys.stderr)
        return 1
    if not (VIEWER_DIR / "index.html").is_file():
        print("error: viewer/index.html is missing. Run python build.py first.", file=sys.stderr)
        return 1

    print(f"A.S.H.R.A {VERSION}")
    print(f"Serving {VIEWER_DIR}")
    print(f"Open in Chrome:  http://{HOST}:{PORT}/")
    print(f"API docs:        http://{HOST}:{PORT}/api/docs")
    print(f"Status: {assistant_status()}")
    print(f"Request limit: {MAX_BODY_BYTES // 1024} KB")
    print("Press Ctrl+C to stop.")
    enable_console_ctrl_c()

    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            log_level="info",
            # A live SSE stream must not hold the process open forever.  Five
            # seconds is longer than any in-flight local answer and short
            # enough that the window closes when asked.
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
