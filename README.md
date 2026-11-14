# A.S.H.R.A

A.S.H.R.A is a local-first personal assistant with a memory of its own. It answers from your Markdown notes and from typed, provenance-carrying memories, shows you its brain, and asks before it acts.

Three surfaces ship today: **Home** (`/`) is the command deck where your brief, inbox, and quick capture live; the **Galaxy** (`/galaxy.html`) maps your notes as a 3D knowledge graph; and the **Brain** (`/brain.html`) lets you inspect, correct, and curate what the assistant remembers.

Designed for personal or small-team use - no public endpoints, no external storage, only your machine. The included `notes/` vault is a sample coffee-business knowledge base; replace it with your own. Deleting the vault no longer erases everything the assistant knows: conversation and capture memory live in local SQLite.

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

Two lanes are configured in `config.json`: a **local** lane that never leaves the machine, and an optional **cloud** lane for public or internal work. The privacy router picks between them per task, and keeps private content local.

```json
{
  "provider": "ollama",
  "model": "qwen3.5:2b-q4_K_M",
  "local": {
    "model": "qwen3.5:2b-q4_K_M",
    "api_base": "http://127.0.0.1:11434",
    "num_ctx": 8192,
    "think": false,
    "timeout_s": 30
  },
  "cloud": {
    "provider": "nvidia",
    "model": "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "credential_ref": "ashra/cloud"
  },
  "privacy": {
    "default_lane": "local",
    "allow_cloud_for": ["public", "internal"],
    "refuse": ["secret"]
  }
}
```

Supported provider values are `ollama`, `anthropic`, `nvidia`, and `openai_compatible`. The local lane needs Ollama running on `127.0.0.1:11434` with the configured model pulled; `GET /health/local` reports `ok`, `down`, or `model_missing`.

No key belongs in `config.json`. Store one in Windows Credential Manager instead:

```bash
python -m ashra.credentials set ashra/cloud   # prompts without echoing
python -m ashra.credentials check ashra/cloud
```

The server re-reads `config.json` on every request, so model and lane changes do not require a restart. `.gitignore` skips the file; never commit a secret.

## Running the app

```bash
# Build the static front-end and index the notes
python build.py

# Start the HTTP server at 127.0.0.1:4700
python server.py
```

Open a browser to http://127.0.0.1:4700/ for Home, http://127.0.0.1:4700/galaxy.html for the Galaxy, or http://127.0.0.1:4700/brain.html for the Brain. Memory and Home work without a provider key; chat needs one.

### API

| Endpoint | Method | Payload | Notes |
|----------|--------|---------|-------|
| `/health` | GET | - | Liveness, version, and server time |
| `/status` | GET | - | Provider, vault, memory, session, tool, agent, and integration summary |
| `/home` | GET | - | One aggregate read for Home: status, brief, Needs You inbox, recent memories, sessions, audit, and stats |
| `/chat` | POST | `{ "question": "...", "session_id": "optional", "permission": "read" }` | Runs the tool loop; returns answer, sources, note ids, proposed memory ids, and any pending approval |
| `/chat/stream` | POST | same as `/chat` | Server-sent events (`start`, `sources`, `tool_call`, `tool_result`, `approval_required`, `token`, `done`, `error`) |
| `/remember` | POST | `{ "text": "...", "source": "typed" }` | Create a new capture |
| `/append` | POST | `{ "text": "...", "last": true, "path": "note.md" }` | Append to an existing capture |
| `/rename` | POST | `{ "title": "New title", "last": true, "path": "note.md" }` | Rename a capture |
| `/forget` | POST | `{ "last": true, "path": "note.md", "confirm": true }` | Delete a capture - requires confirmation |
| `/sessions/{id}` | GET | - | Persisted conversation history for one session |
| `/tools` | GET | `?permission=read` | Tools available at a permission level |
| `/tools/invoke` | POST | `{ "name": "...", "arguments": {}, "permission": "read", "approved": false }` | Invoke a tool through the permission gate |
| `/agents` | GET | - | Specialist subagents, their permission ceilings, and tool scopes |
| `/agents/{name}` | GET | - | One subagent definition |
| `/integrations` | GET | - | Connected MCP servers, their tools, and any connection errors |
| `/integrations/{name}` | GET | - | Status for one MCP integration |
| `/connectors` | GET | - | Declared external connectors, their capabilities, health, and credential readiness (names only) |
| `/connectors/{id}` | GET | - | One connector declaration and its last-known health |
| `/approvals` | GET | `?status=pending&limit=50` | Pending and recent approval requests |
| `/approvals/{id}` | POST | `{ "approve": true }` | Approve or deny a pending tool call and resume the answer |
| `/audit` | GET | `?limit=50&event=chat` | Recent audit events |
| `/memory` | GET | `?status=&kind=&limit=` | Memories plus global lifecycle stats |
| `/memory` | POST | `{ "content": "...", "kind": "fact", "status": "proposed" }` | Create one memory |
| `/memory/stats` | GET | - | Lifecycle counts and health signals |
| `/memory/proposals` | GET | `?limit=100` | Proposed memories awaiting review |
| `/memory/search` | GET | `?q=...&status=confirmed&kind=` | Scored lexical search |
| `/memory/graph` | GET | `?status=&limit=500` | Memory nodes and shared-entity links |
| `/memory/timeline` | GET | `?status=&limit=120` | Memories in chronological order |
| `/memory/propose` | POST | `{ "text": "..." }` | Split text into atomic proposals |
| `/memory/remember` | POST | `{ "text": "..." }` | Split text into atomic memories and confirm them immediately |
| `/memory/ingest` | POST | - | Import notes as `document` memories |
| `/memory/consolidate` | POST | - | Merge duplicates and decay stale records |
| `/memory/{id}` | GET | - | One memory plus related memories |
| `/memory/{id}/confirm` | POST | - | Confirm a proposal |
| `/memory/{id}/reject` | POST | - | Reject a proposal |
| `/memory/{id}` | PATCH | `{ "content": ..., "pinned": true }` | Edit or pin a memory |
| `/memory/{id}/forget` | POST | `{ "confirm": true }` | Permanently delete a memory |
| `/rebuild` | POST | - | Rebuild `viewer/graph-data.js` from the notes vault |

All responses are JSON except `/chat/stream`, which uses `text/event-stream`. The server only serves files inside `viewer/`; requests for `config.json`, `.env`, or `.git` are blocked. Every response includes an `X-Request-ID` header.

### Persistence and audit

Conversations, memory, approvals, and audit events live in `data/ashra.db` (SQLite, git-ignored). The database stores sessions, messages, typed memories, memory links and events, and audit records for chat, vault, memory, and tool actions. The in-memory `SessionStore` remains available for tests and scripts; the API injects the SQLite-backed store.

### Memory

Memory is not a transcript and not a folder of notes. It is a set of small, typed claims with provenance and a lifecycle:

```text
proposed -> confirmed -> archived
         \-> rejected
```

Kinds are `fact`, `preference`, `person`, `project`, `event`, `task`, `document`, `procedure`, and `observation`. Every record carries its source, confidence, sensitivity, reinforcement count, entities, and timestamps.

- **Propose and confirm.** By default A.S.H.R.A proposes memories and waits for approval in Home's Needs You queue or the Brain's proposals inbox. An explicit "remember that …" capture is confirmed immediately. A conservative detector also proposes durable first-person statements ("I prefer …", "we ship on …") while ignoring questions and commands.
- **Quick capture.** Home's capture box can either propose or remember in one step, with an explicit kind. The Galaxy's answer card offers **Remember this**, which splits a long answer into atomic memories.
- **Retrieval.** Search scores lexical overlap together with confidence, reinforcement, recency, and pinning. FTS5 is used when the local SQLite build supports it; otherwise the store falls back to a scored scan.
- **Evidence.** Chat sources include memory records alongside notes, so an answer can show what it remembered and where that came from.
- **Lifecycle.** Confirm, reject, edit, pin, archive, consolidate, and forget are first-class operations. Forgetting is permanent and requires confirmation.
- **Health.** The Brain reports stale, orphaned, low-confidence, and duplicate records.
- **Secrets are refused.** Credential-shaped text never enters memory.
- **Notes are a connector.** Ingesting the vault creates `document` memories with a stable `notes/...` reference. Deleting the vault leaves conversation and capture memory intact.

The **Home** hub at `/` is the control surface: a daily brief, a Needs You queue for proposed memories and pending approvals, quick capture, recent threads, recently confirmed memories, and recent activity, plus a Ctrl+K command palette.

The **Brain** at `/brain.html` exposes three synchronized views over the same data: a force-directed **Graph** of shared entities, a **Timeline** of when memories arrived, and a **Library** with search, filters, provenance, and lifecycle actions.

### Tools and permissions

Tools declare a permission tier and whether they need approval:

| Tier | Meaning |
|------|---------|
| `read` | Inspect notes or files |
| `draft` | Prepare text without sending it |
| `act` | Change local state |
| `admin` | Destructive or configuration-level work |

The default tool pack is `fs_list`, `fs_read`, and `fs_search`, scoped to `notes/`, plus `spawn_agent` for delegation. `vault_remember` and `vault_append` require `act` permission plus `approved: true`. The memory pack adds `memory_search` (`read`), `memory_propose` (`draft`), `memory_remember` (`act` plus approval), and `memory_forget` (`admin` plus approval). Paths cannot escape the vault, and blocked names such as `config.json`, `.env`, and `.git` are never exposed.

### Tool-calling orchestrator

`/chat` and `/chat/stream` run a bounded loop: retrieve notes, ask the model, execute any tools it requests through the permission gate, feed the results back, and repeat. The loop stops after six model turns, so a confused model cannot run forever.

Chat requests carry a `permission` tier (`read` by default; the viewer sends `act`). Read tools run immediately. A state-changing tool that requires approval does not run: the orchestrator stores the pending call in `data/ashra.db`, emits `approval_required`, and returns `status: "awaiting_approval"`. Approving the call through `POST /approvals/{id}` executes it and resumes the answer with the saved conversation state.

Providers that do not implement tool calling still answer normally, just without tools. The Anthropic and OpenAI-compatible providers both support tool schemas and tool results.

### Scoped subagents

The orchestrator can delegate one bounded task to a specialist through the `spawn_agent` tool. Phase 1E ships three:

| Agent | Permission ceiling | Scope |
|-------|--------------------|-------|
| `researcher` | `read` | Search the vault and read-only integrations, then report the evidence. |
| `writer` | `draft` | Turn supplied material into a draft; never sends anything. |
| `operator` | `act` | Inspect state and prepare actions; approval-gated calls go back to the parent. |

Three rules bound every child:

- The child's permission is the weaker of its own declaration and the parent's `permission`. An `operator` spawned by a `read` caller runs as `read`.
- The child only sees tools matching its allowlist. `spawn_agent` is removed from the child's tool list, so delegation cannot recurse.
- Approval-gated tools are never advertised to a child. If the model asks for one anyway, the call is refused and the child is told to return the proposed action to the parent, which owns the approval queue.

Each run returns `{ "agent": ..., "status": ..., "answer": ..., "steps": ..., "tools": [...], "permission": ... }` and writes an `agent` audit event. `GET /agents` lists the registry.

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

### Connectors (read-only foundation)

Phase 2E.0 adds the contract every external adapter implements. There is no Gmail or calendar adapter yet; the point of this phase is that adding one cannot widen its own authority.

```python
from ashra.connectors import Connector, PollResult, Record, Source, NormalisedRecord

class GmailConnector(Connector):
    def __init__(self, vault):
        super().__init__(
            connector_id="gmail",
            label="Gmail inbox",
            capabilities={"read": True},          # no draft, write, or delete
            scopes={"folders": ["INBOX"]},
            credential_ref="ashra/gmail",         # a name, never a value
            min_interval_seconds=60,
        )
        self.vault = vault

    def poll(self, cursor=None):
        ...

    def normalise(self, record):
        return NormalisedRecord(source=Source(...), candidates=(...))
```

The rules the registry enforces:

- **Declared capabilities only.** A connector that declares `read` and is asked to `write` raises `CapabilityDenied`. `delete` cannot be declared without `write`, and nothing in v1.0 may declare either.
- **Polling is idempotent.** Sources are deduplicated by `(connector, kind, external_id)`, so a repeated poll updates the row instead of duplicating the mailbox.
- **Polls are rate-limited** per connector, and a failure is recorded as health rather than raised into chat.
- **Classification happens at ingestion.** Each Source carries one of the PRD classes (`public`, `internal`, `private`, `sensitive`, `secret`), inferred conservatively when the adapter does not declare one, and a candidate proposal inherits the stronger of its own and its source's class.
- **Proposals, not writes.** `normalise()` returns candidate tasks and memories. They are returned to the caller for confirmation; nothing enters the task ledger or memory store directly.
- **Credential references, not values.** `GET /connectors` reports whether `ashra/gmail` resolves. The vault is Windows Credential Manager on Windows and an in-memory vault elsewhere, which is deliberately not persistent.

Schema v4 adds `connectors` and `sources`, and migrates the old three-value memory sensitivity vocabulary (`normal`, `private`, `sensitive`) to the PRD's five classes (`normal` becomes `internal`). `ReplayConnector` feeds fixture records through the same path, so the whole loop is testable without a mailbox or a key.

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

- Chat with your notes and memory: `POST /chat` retrieves both, then asks the LLM.
- Home hub: a daily brief, Needs You queue, quick capture, recent threads, and recent activity in one aggregate read.
- Typed long-term memory with a propose/confirm lifecycle, provenance, and hybrid-ready retrieval.
- The Brain: graph, timeline, and library views with a proposals inbox and confirm/reject/pin/edit/forget actions.
- Automatic memory proposals for durable first-person statements, plus one-click "Remember this" on answers.
- Note ingestion into memory, so the vault is a source rather than the substrate.
- Tool-calling loop: the assistant can search and read the vault and memory on its own, then answer.
- Scoped subagents: delegate to `researcher`, `writer`, or `operator` with permission ceilings and tool allowlists.
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
- `ashra/db.py` - SQLite sessions, messages, memories, approvals, and audit events.
- `ashra/memory.py` - typed memory store, lifecycle, retrieval, graph, ingestion, and health.
- `ashra/tools.py` - permission tiers, tool registry, scoped filesystem tools, and the memory tool pack.
- `ashra/agents/` - specialist registry, permission ceiling, bounded child runner, and `spawn_agent`.
- `ashra/integrations/` - MCP client, integration config, and tool registration.
- `ashra/mcp_server.py` - optional read-only MCP server for the notes vault.
- `ashra/remember.py` - captures, appends, renames, and deletes note files.
- `ashra/config.py` - loads and validates `config.json`.
- `viewer/` - static front-end assets: shared shell (`chrome.css`, `chrome.js`), Home (`index.html`, `home.css`, `home.js`), Galaxy (`galaxy.html`, `app.js`, `styles.css`), Brain (`brain.html`, `brain.js`, `brain.css`), and pinned 3D libraries in `vendor/`.
- `docs/design/aurora-rail.md` - the shared UI design system: tokens, typography, components, surface rules, responsive breakpoints, and accessibility conventions.

## Contributing

1. Fork the repository.
2. Create a branch.
3. Add your changes and tests.
4. Submit a pull request.

All contributions should pass the test suite.

## License

MIT (c) 2026 A.S.H.R.A Project.
