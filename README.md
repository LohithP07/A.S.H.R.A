# A.S.H.R.A

A.S.H.R.A is a local-first, note-grounded personal assistant. It exposes a simple UI in `viewer/`, lets you store and query Markdown notes, and answers questions by searching those notes with an LLM.

Designed for personal or small-team use - no public endpoints, no external storage, only your machine. The included `notes/` vault is a sample coffee-business knowledge base; replace it with your own.

## Installation

```bash
git clone https://github.com/LohithP07/A.S.H.R.A.git
cd A.S.H.R.A

# Optional: create a virtual environment
python -m venv .venv
.\.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # macOS/Linux
```

Install the backend dependencies:

```bash
python -m pip install -e ".[dev]"
```

The core API uses FastAPI and Uvicorn. The optional MCP filesystem server uses the MCP SDK, which is included in the `dev` extra. The viewer loads Three.js and 3d-force-graph from a CDN.

## Configuration

The LLM provider is configured in `config.json`:

```json
{
  "provider": "openai_compatible",
  "model": "YOUR_MODEL_ID",
  "api_key": "YOUR_API_KEY",
  "api_base": "https://your-provider.example/v1"
}
```

Supported provider values are `anthropic`, `nvidia`, and `openai_compatible`. Use `openai_compatible` for providers that expose an OpenAI-style `/chat/completions` endpoint.

Copy `config.example.json` to `config.json` and fill in your key. Never commit secrets - `.gitignore` already skips it. The server re-reads `config.json` on every request, so key and model changes do not require a restart.

## Running the app

```bash
# Build the static front-end and index the notes
python build.py

# Start the HTTP server at 127.0.0.1:4700
python server.py
```

Open a browser to http://127.0.0.1:4700/ to see the A.S.H.R.A UI.

### API

| Endpoint | Method | Payload | Notes |
|----------|--------|---------|-------|
| `/health` | GET | - | Liveness, version, and server time |
| `/status` | GET | - | Provider, vault, session, and tool summary |
| `/chat` | POST | `{ "question": "...", "session_id": "optional", "permission": "read" }` | Runs the tool loop; returns answer, sources, note ids, and any pending approval |
| `/chat/stream` | POST | same as `/chat` | Server-sent events (`start`, `sources`, `tool_call`, `tool_result`, `approval_required`, `token`, `done`, `error`) |
| `/remember` | POST | `{ "text": "...", "source": "typed" }` | Create a new capture |
| `/append` | POST | `{ "text": "...", "last": true, "path": "note.md" }` | Append to an existing capture |
| `/rename` | POST | `{ "title": "New title", "last": true, "path": "note.md" }` | Rename a capture |
| `/forget` | POST | `{ "last": true, "path": "note.md", "confirm": true }` | Delete a capture - requires confirmation |
| `/sessions/{id}` | GET | - | Persisted conversation history for one session |
| `/tools` | GET | `?permission=read` | Tools available at a permission level |
| `/tools/invoke` | POST | `{ "name": "...", "arguments": {}, "permission": "read", "approved": false }` | Invoke a tool through the permission gate |
| `/integrations` | GET | - | Connected MCP servers, their tools, and any connection errors |
| `/integrations/{name}` | GET | - | Status for one MCP integration |
| `/approvals` | GET | `?status=pending&limit=50` | Pending and recent approval requests |
| `/approvals/{id}` | POST | `{ "approve": true }` | Approve or deny a pending tool call and resume the answer |
| `/audit` | GET | `?limit=50&event=chat` | Recent audit events |
| `/rebuild` | POST | - | Rebuild `viewer/graph-data.js` from the notes vault |

All responses are JSON except `/chat/stream`, which uses `text/event-stream`. The server only serves files inside `viewer/`; requests for `config.json`, `.env`, or `.git` are blocked. Every response includes an `X-Request-ID` header.

### Persistence and audit

Conversations and audit events live in `data/ashra.db` (SQLite, git-ignored). The database stores sessions, messages, and audit events for chat, vault, and tool actions. The in-memory `SessionStore` remains available for tests and scripts; the API injects the SQLite-backed store.

### Tools and permissions

Tools declare a permission tier and whether they need approval:

| Tier | Meaning |
|------|---------|
| `read` | Inspect notes or files |
| `draft` | Prepare text without sending it |
| `act` | Change local state |
| `admin` | Destructive or configuration-level work |

The default tool pack is `fs_list`, `fs_read`, and `fs_search`, scoped to `notes/`. `vault_remember` and `vault_append` require `act` permission plus `approved: true`. Paths cannot escape the vault, and blocked names such as `config.json`, `.env`, and `.git` are never exposed.

### Tool-calling orchestrator

`/chat` and `/chat/stream` run a bounded loop: retrieve notes, ask the model, execute any tools it requests through the permission gate, feed the results back, and repeat. The loop stops after six model turns, so a confused model cannot run forever.

Chat requests carry a `permission` tier (`read` by default; the viewer sends `act`). Read tools run immediately. A state-changing tool that requires approval does not run: the orchestrator stores the pending call in `data/ashra.db`, emits `approval_required`, and returns `status: "awaiting_approval"`. Approving the call through `POST /approvals/{id}` executes it and resumes the answer with the saved conversation state.

Providers that do not implement tool calling still answer normally, just without tools. The Anthropic and OpenAI-compatible providers both support tool schemas and tool results.

### MCP integrations (read-only)

A.S.H.R.A can attach external MCP servers and expose their tools through the same permissioned registry. Phase 1D is read-only: integrations must declare `permission: "read"`, and their tools never require approval because they cannot change state.

Add an `integrations` array to `config.json`:

```json
{
  "integrations": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": ["python", "-m", "ashra.mcp_server", "--root", "notes"],
      "permission": "read",
      "timeout": 20
    },
    {
      "name": "calendar",
      "transport": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "permission": "read"
    }
  ]
}
```

- `name` becomes the tool prefix: a remote `search` tool from `gmail` is exposed as `gmail_search`.
- `transport` is `stdio` or `http` (also accepts `streamable_http`).
- `stdio` requires `command`; `http` requires `url`.
- `env` and `cwd` are optional for stdio servers.
- `timeout` is per operation, in seconds (1-120).

Each server is probed once at startup. Restart the server after changing the `integrations` array. A broken server is reported by `GET /integrations` and does not block chat or the other integrations. Tool calls are audited like local tools, and the model sees remote tools through the normal bounded orchestrator loop.

The MCP SDK is optional. Install it with:

```bash
python -m pip install "mcp>=1.0"
```

### MCP filesystem server

The same scoped reads are available over MCP stdio:

```bash
python -m ashra.mcp_server --root notes
# or, after installation:
ashra-mcp-filesystem --root notes
```

## Notes vault

Notes live as Markdown files under `notes/`. Example categories include:

- `notes/strategy/`
- `notes/people/`
- `notes/marketing/`
- `notes/operations/`
- `notes/products/`
- `notes/customers/`
- `notes/captures/` (generated when notes are created)

You can name ordinary notes however you like. Captures created through `/remember` are written as:

```text
notes/captures/<slug>.md
```

Each capture contains simple frontmatter with `created` and `source`, an H1 title, the note body, and a `Related: [[Title]]` link when a related note is found.

## Features

- Chat with your notes: `POST /chat` builds a graph of related notes and asks the LLM.
- Tool-calling loop: the assistant can search and read the vault on its own, then answer.
- Read-only MCP integrations: attach mail, calendar, or other MCP servers as namespaced tools.
- Streaming answers: `POST /chat/stream` sends sources first, then word-sized tokens.
- Approval gate: state-changing tools pause for a click in the viewer before running.
- Automatic graph generation: `build.py` produces `viewer/graph-data.js` from all notes.
- Note CRUD through `/remember`, `/append`, `/rename`, and `/forget`.
- Durable sessions and an audit log in local SQLite.
- Permissioned tools with approval gates for writes.
- Optional scoped filesystem MCP server.
- Secure by design: local-only server, no open endpoints, no keys in responses or logs.
- Configurable provider: Anthropic, NVIDIA NIM, and any OpenAI-compatible endpoint via `config.json`.

## Development

| Task | Command |
|------|---------|
| Install for development | `python -m pip install -e ".[dev]"` |
| Start the server | `python server.py` |
| Rebuild the viewer index | `python build.py` |
| Run tests | `python -m pytest -q` or `python -m unittest discover -v` |
| Compile check | `python -m compileall -q ashra build.py server.py` |
| Run the MCP filesystem server | `python -m ashra.mcp_server --root notes` |

To add a new note category:

1. Create a Markdown file under the appropriate folder, e.g. `notes/marketing/new-plan.md`.
2. Run `python build.py` to rebuild the graph index.

## Under the hood

- `server.py` - Uvicorn entrypoint and startup banner.
- `ashra/api.py` - FastAPI routes, SSE streaming, request IDs, and error mapping.
- `ashra/chat.py` - `ChatService.ask()` and `ChatService.stream()` share retrieval and provider logic.
- `ashra/orchestrator.py` - bounded tool-calling loop and approval continuation.
- `ashra/db.py` - SQLite sessions, messages, approvals, and audit events.
- `ashra/tools.py` - permission tiers, tool registry, and scoped filesystem tools.
- `ashra/integrations/` - MCP client, integration config, and tool registration.
- `ashra/mcp_server.py` - optional read-only MCP server for the notes vault.
- `ashra/remember.py` - captures, appends, renames, and deletes note files.
- `ashra/config.py` - loads and validates `config.json`.
- `viewer/` - static front-end assets.

## Contributing

1. Fork the repository.
2. Create a branch.
3. Add your changes and tests.
4. Submit a pull request.

All contributions should pass the test suite.

## License

MIT (c) 2026 A.S.H.R.A Project.
