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

# Exposed for ``uvicorn server:app`` and for tests.
app = create_app(BASE_DIR)


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

    print(f"A.S.H.R.A {VERSION}")
    print(f"Serving {VIEWER_DIR}")
    print(f"Open in Chrome:  http://{HOST}:{PORT}/")
    print(f"API docs:        http://{HOST}:{PORT}/api/docs")
    print(f"Status: {assistant_status()}")
    print(f"Request limit: {MAX_BODY_BYTES // 1024} KB")
    print("Press Ctrl+C to stop.")

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
