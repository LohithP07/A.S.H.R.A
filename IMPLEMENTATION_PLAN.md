# A.S.H.R.A / Personal Assistant — Implementation Plan

## North star

Start from the Prompt Pack's 3D knowledge galaxy, add a note-grounded assistant,
then grow it into a personal assistant that can use tools, remember context,
and eventually work outside this browser demo.

The long-term target is **not** just "a chat box over notes." It is a personal
assistant with:

- a private, editable knowledge base (the current `notes/` vault)
- a model-agnostic reasoning layer
- persistent conversation memory
- voice input/output
- safe tool/action execution with confirmation gates
- a clear separation between public UI, private configuration, and secrets

This plan keeps those seams in place now so the later conversion is an
extension, not a rewrite.

> **The PRD is authoritative for v1.0.** `docs/prd/ashra-prd.pdf` (v1.0 Draft,
> 13 November 2026) defines scope, privacy classes, connector and channel
> contracts, and the milestone plan. This file records how that plan is being
> executed; where the two disagree, the PRD wins and this file is corrected.

---

## Current baseline

**Prompt 1 — The Galaxy: complete.**

- `build.py` scans `notes/**/*.md` with the standard library and writes
  `viewer/graph-data.js`.
- 26 notes, 95 links, ~666-character average excerpts.
- Every node id equals its index in the nodes array.
- `viewer/index.html` + `app.js` render a cinematic 3D force graph from CDN
  libraries, with starfield, glow, color groups, idle drift, camera flights,
  neighbor highlighting, and a note panel.
- `server.py` serves only `viewer/` on `127.0.0.1:4700`.

**Prompt 2 — The Assistant: implemented in this pass.**

- `config.json` in the project root (gitignored; the key stays server-side).
- Current configuration targets NVIDIA NIM with
  `nvidia/nemotron-3.5-super-vl-120b-a12b`; Anthropic remains supported.
- Server-side note loading, keyword retrieval, session history, model API
  call, and `POST /chat`.
- Viewer input bar with thinking status, answer card, and source labels.
- No API key is ever sent to the browser or placed in a served file.

---

## Phase 0 — Foundation (complete)

- [x] Use A.S.H.R.A as the project and assistant name across the UI, persona,
      session keys, capture prefixes, server banner, and documentation.
- [x] Rename the Python package from `brain/` to `ashra/`.
- [x] Fix the README configuration example (`api_key`, supported providers,
      `api_base`) and the capture filename documentation.
- [x] Make small talk work before a provider key is configured.
- [x] Add a `tests/` suite covering config, retrieval, chat, capture CRUD, and
      the HTTP server.
- [x] Add a GitHub Actions CI workflow.
- [x] Add `pyproject.toml`, `LICENSE`, and development tooling metadata.
- [x] Keep the Phase 0 backend dependency-free.

---

## Architecture principles

1. **Keep secrets server-side.** `config.json` lives outside `viewer/`; the
   viewer server is rooted at `viewer/`. The server also explicitly refuses
   requests for `config.json` and `.env`.
2. **Keep the assistant modular.** Retrieval, sessions, provider transport, and
   configuration are separate modules. A future personal assistant can replace
   one without touching the others.
3. **Keep the browser contract stable.** The viewer only knows
   `POST /chat -> {answer, nodes, sources, session_id}`. New backends, tools, or
   models should preserve that shape where possible.
4. **Prefer stdlib until a dependency earns its place.** Phase 0 stayed
   dependency-free. Phase 1 added FastAPI/Uvicorn for the API and SQLite (still
   stdlib) for persistence; the MCP SDK remains optional.
5. **Ground answers in notes.** The system prompt forbids outside knowledge and
   requires the assistant to admit when the notes do not cover a question.

---

## Architecture

```text
viewer/ (public)
  index.html
  app.js  ── POST /chat, /chat/stream ──┐
  styles.css              │
                          ▼
server.py (Uvicorn) ── ashra/api.py ── ashra/orchestrator.py
                                        ├── chat.py       retrieval + prompt pipeline
                                        ├── tools.py      permissions + registry
                                        ├── agents/       specialist registry + child runner
                                        ├── integrations/ MCP client + tool registration
                                        ├── db.py         SQLite sessions, approvals + audit
                                        ├── notes.py      load + clean markdown
                                        ├── retrieval.py  keyword scoring -> top 6
                                        ├── sessions.py   store protocol + in-memory
                                        ├── providers.py  Anthropic / NVIDIA NIM + tools
                                        └── config.py     config.json validation

ashra/mcp_server.py ── scoped read-only filesystem tools
```

`config.json` and `notes/` stay outside the served tree.

---

## Phase 1 — Foundation (complete)

- [x] Replace the standard-library HTTP server with FastAPI + Uvicorn while
      keeping `python server.py` as the entrypoint.
- [x] Preserve `/chat`, `/remember`, `/append`, `/rename`, `/forget`, and static
      viewer serving.
- [x] Add `/health`, `/status`, `/chat/stream` (SSE), `/sessions/{id}`,
      `/tools`, `/tools/invoke`, `/audit`, and `/rebuild`.
- [x] Add request IDs, structured request logging, payload limits, and blocked
      secret paths.
- [x] Add SQLite persistence for sessions, messages, and audit events in
      `data/ashra.db`.
- [x] Inject `SQLiteSessionStore` into `ChatService` behind the existing
      `ensure` / `history` / `append_exchange` contract.
- [x] Add permission tiers (`read`, `draft`, `act`, `admin`), a tool registry,
      schema validation, and approval gates.
- [x] Add scoped filesystem tools (`fs_list`, `fs_read`, `fs_search`) rooted at
      `notes/`, with traversal and blocked-name protection.
- [x] Add approval-gated `vault_remember` and `vault_append` tools.
- [x] Add an optional read-only MCP filesystem server compatible with MCP 1.x
      and 2.x.
- [x] Stream answers in the viewer over SSE with a `/chat` fallback.
- [x] Add tests for the API, database, tools, MCP server, and streaming.

---

## Phase 1B — The Assistant (current)

### 1. Configuration

- [x] Create root `config.json` with `api_key`, `model`, `provider`, and tuning
      fields.
- [x] Validate and bound config values in `ashra/config.py`.
- [x] Re-read config per request so the user can paste a key without restarting.
- [x] Never include the key in API responses, HTML, or served files.
- [x] Return a clear `model_not_configured` response when the placeholder is
      still present.

### 2. Note index

- [x] Load notes from the same `build.py` scanner used by the galaxy.
- [x] Keep title, label, group, relative path, excerpt, full cleaned text, and
      word count.
- [x] Keep node ids aligned with the graph's array indexes.
- [ ] Future: cache the index and reload when a note's mtime changes
      (needed for Prompt 6 live capture).

### 3. Retrieval

- [x] Tokenize questions, drop common stopwords, and handle `P&L` as `pl`.
- [x] Score title/label matches highest, then path, group, and body terms.
- [x] Add phrase bonuses for exact question/title or question/body matches.
- [x] Include the last two user questions at lower weight so follow-ups such as
      "what about wholesale?" still retrieve relevant notes.
- [x] Return the configured top 6 notes (default `max_notes: 6`).
- [ ] Future: add embeddings/vector search behind the same `score_notes`
      contract if keyword search becomes too shallow.

### 4. Provider

- [x] Define a `ChatProvider` protocol.
- [x] Implement `AnthropicProvider` with `urllib` and the Messages API.
- [x] Implement `OpenAICompatibleProvider` for NVIDIA NIM and other
      `/v1/chat/completions` endpoints.
- [x] Support `provider: "anthropic"`, `provider: "nvidia"`, and
      `provider: "openai_compatible"`.
- [x] Surface provider errors without leaking the API key.
- [x] Handle reasoning models: if internal reasoning exhausts `max_tokens`
      before any visible answer, retry once with a 4096-token budget and then
      report a clear error.
- [ ] Future: add more backends by extending `ashra/providers.py`.

### 5. Sessions

- [x] Thread-safe in-memory `SessionStore`.
- [x] Client sends a `session_id`; server creates/validates one.
- [x] Keep the last 8 messages by default (about 4 turns).
- [x] Include history in the provider message list so follow-ups work.
- [x] Persist sessions and messages in SQLite behind the same
      `ensure`, `history`, and `append_exchange` methods.
- [x] Keep the in-memory store for tests and scripts.
- [ ] Future: add export/backup and per-session deletion commands.

### 6. `POST /chat`

- [x] Accept `{question, session_id?}`.
- [x] Validate JSON, content length, question type, and session id type.
- [x] Return `{answer, nodes, sources, session_id, model}`.
- [x] Map errors to `400` / `500` / `502` / `503` with JSON details.
- [x] Do not log request bodies or secrets.
- [x] Keep `GET` static serving for the viewer.
- [x] Add `POST /chat/stream` with `start`, `sources`, `token`, `done`, and
      `error` SSE events.
- [x] Record chat outcomes in the audit log.

### 7. Viewer chat UI

- [x] Add a bottom input bar and Ask button.
- [x] Show a thinking status, answer card, and source labels.
- [x] Keep the session id in `sessionStorage`.
- [x] Expose `window.__ashra.ask(question)` for future voice and automation.
- [x] Make the bar responsive and hide the legend on narrow screens.
- [ ] Future: add markdown rendering if answers ever include rich formatting.
      (Current answers are plain text by design.)

### 8. Verification

- [x] Compile all Python modules.
- [x] Test retrieval against real notes.
- [x] Test the full pipeline with a fake provider (no network/key required).
- [x] Test `503 model_not_configured` with the placeholder key.
- [x] Test static viewer serving, malformed JSON, and blocked config paths.

### Phase 1 acceptance criteria

- `python server.py` starts on port 4700 and prints the assistant status.
- The viewer loads and the input bar is usable.
- With a valid key in `config.json`, a question returns a 2–3 sentence answer
  grounded in the notes, plus the source node indexes.
- With the placeholder key, the UI says the assistant is not configured instead of
  failing silently.
- Follow-up questions in the same tab use the short server-side history.
- No secret appears in browser DevTools, HTML, or a served file.

---

## Phase 1C — The Orchestrator (complete)

- [x] Add a bounded tool-calling loop in `ashra/orchestrator.py`
      (six model turns, six tool calls per turn).
- [x] Share retrieval and grounding with `ChatService.prepare()` so tool answers
      use the same note context as plain answers.
- [x] Add provider tool support for Anthropic (`tools` / `tool_use`) and
      OpenAI-compatible endpoints (`tools` / `tool_calls`).
- [x] Convert `ToolSpec` schemas to each provider's tool format.
- [x] Filter advertised tools by the caller's permission tier.
- [x] Execute tool calls through `ToolRegistry` and feed success or error
      outcomes back to the model.
- [x] Add a persisted approval queue (`approvals` table) for state-changing
      tools, with atomic approve/deny and a 15-minute expiry.
- [x] Add `GET /approvals`, `GET /approvals/{id}`, and `POST /approvals/{id}`.
- [x] Add `tool_call`, `tool_result`, and `approval_required` SSE events, plus
      `status` on the `done` event.
- [x] Resume a paused answer from saved conversation state after approval, and
      cancel cleanly on denial.
- [x] Add the approval prompt and Approve/Deny buttons to the viewer.
- [x] Fall back to a single model call when a provider has no tool support.
- [x] Audit tool calls, approval creation, and approval decisions.
- [x] Test the loop, bounds, approvals, denial, resume, schemas, and API paths.

### Phase 1C acceptance criteria

- A question that needs the vault runs `fs_search` without being told to.
- A state-changing tool pauses with `status: "awaiting_approval"` and writes
  nothing until `POST /approvals/{id}` approves it.
- Denying the approval runs no tool and returns a clear answer.
- The loop stops at six steps with a readable message instead of spinning.
- A provider without tool support still answers as it did in Phase 1B.

---

## Phase 1D — MCP integrations (complete)

- [x] Add `ashra/integrations/` with config parsing, an MCP client boundary, and
      an integration manager.
- [x] Support `stdio` and streamable HTTP transports against MCP 1.x and 2.x.
- [x] Read the optional `integrations` array from `config.json`, with validation
      for names, transports, commands, URLs, timeouts, and env.
- [x] Enforce read-only integrations in Phase 1D; write permissions are rejected
      until the draft/act layer exists.
- [x] Probe every enabled server at startup and record per-server health.
- [x] Register remote tools as namespaced `ToolSpec`s (`gmail_search`) under the
      caller's permission tier.
- [x] Keep a broken server from blocking chat or other integrations.
- [x] Add `GET /integrations` and `GET /integrations/{name}`.
- [x] Include integration status in `GET /status`.
- [x] Audit remote tool calls through the existing registry audit path.
- [x] Test config parsing, failure isolation, namespacing, registration, API
      paths, and a real stdio round-trip against `ashra.mcp_server`.

### Phase 1D acceptance criteria

- A configured read-only MCP server appears in `GET /integrations` with its
  tools and connection state.
- Its tools are listed by `GET /tools` and callable through `POST /tools/invoke`.
- A dead server reports an error in `/integrations` while the rest of A.S.H.R.A
  keeps working.
- `permission: "act"` on an integration is rejected with a clear config error.
- The full suite passes with and without the MCP SDK installed (the real SDK
  test skips when it is absent).

---

## Phase 1E — Scoped subagents (complete)

- [x] Add `ashra/agents/` with an `AgentSpec`, an `AgentRegistry`, and a
      `SubagentRunner`.
- [x] Ship three default specialists: `researcher` (`read`), `writer`
      (`draft`), and `operator` (`act`).
- [x] Expose a `spawn_agent` tool that accepts an agent name and one task.
- [x] Enforce a permission ceiling: the child holds the weaker of its declared
      permission and the parent's granted permission.
- [x] Enforce a per-agent tool allowlist with glob patterns
      (`fs_*`, `*_search`).
- [x] Remove `spawn_agent` from child tool lists so delegation cannot recurse.
- [x] Refuse approval-gated tools in children and return the proposed action to
      the parent, which owns the approval queue.
- [x] Return a structured result (`agent`, `status`, `answer`, `steps`,
      `tools`, `permission`) and write one `agent` audit event per run.
- [x] Add ContextVars for the current permission and request id, set around
      `/tools/invoke` and every orchestrator tool execution.
- [x] Add `GET /agents` and `GET /agents/{name}`, and include the agent count in
      `GET /status`.

### Phase 1E acceptance criteria

- A `read` caller can spawn `researcher`; the child runs read tools and returns
  a structured summary.
- An `operator` spawned by a `read` caller runs as `read` and cannot execute an
  `act` tool.
- An approval-gated tool is never advertised to a child; a hallucinated call is
  refused with an instruction to delegate the proposal to the parent.
- A child never sees `spawn_agent` in its tool schemas.
- The child loop stops at its step limit with a readable status instead of
  spinning.
- `GET /agents` lists the specialists and `GET /status` reports the count.
- The full suite passes.

---

## Phase 2A — Memory substrate (complete)

The notes vault is now one source of knowledge, not the whole mind. Memory is a
set of small, typed, provenance-carrying claims with a lifecycle.

- [x] Add schema v3 with `memories`, `memory_links`, `memory_events`, and an
      optional FTS5 index. When FTS5 is unavailable the store falls back to a
      scored full scan.
- [x] Add `ashra/memory.py` with typed kinds (`fact`, `preference`, `person`,
      `project`, `event`, `task`, `document`, `procedure`, `observation`) and
      statuses (`proposed`, `confirmed`, `rejected`, `archived`).
- [x] Support `propose`, `remember`, `confirm`, `reject`, `archive`, `update`,
      and `forget`; duplicate claims reinforce the existing record instead of
      creating noise.
- [x] Score retrieval by lexical overlap, confidence, reinforcement, recency,
      and pinning. Return evidence, not just text.
- [x] Extract entities and link memories that share them, so the Brain graph has
      real edges rather than decorative ones.
- [x] Ingest notes as `document` memories with a stable `notes/...` source
      reference; re-ingestion updates instead of duplicating.
- [x] Report lifecycle counts and health signals: stale, orphaned,
      low-confidence, and duplicate groups.
- [x] Consolidate exact duplicates and gently decay memories nobody has seen.
- [x] Reject credential-shaped text so memory can never become a secret store.
- [x] Wire memory into chat: retrieval runs before the model call, memory is
      included in the system prompt, and memory-only answers work without a
      matching note.
- [x] Add memory tools: `memory_search` (`read`), `memory_propose` (`draft`),
      `memory_remember` (`act` + approval), `memory_forget` (`admin` +
      approval).
- [x] Add the `/memory` API: list, stats, proposals, search, graph, timeline,
      create, propose, ingest, consolidate, get, confirm, reject, patch, forget.
- [x] `POST /remember` also creates confirmed memory from an explicit capture,
      while refusing credential-shaped captures.

### Phase 2A acceptance criteria

- A proposed memory is invisible to retrieval until confirmed.
- Confirming a proposal makes it retrievable; rejecting it removes it from the
  queue without deleting the audit trail.
- A duplicate claim reinforces the original rather than creating a second row.
- Notes ingest idempotently and appear as `document` memories.
- Chat can answer from memory when no note matches, and cites memory sources.
- Memory tools respect `read`, `draft`, `act`, and `admin` gates.
- Secrets are refused at the store boundary.
- The full suite passes.

---

## Phase 2B — The Brain UI (complete)

The Brain is a place to inspect, correct, and trust what the assistant knows —
not a prettier graph.

- [x] Add `viewer/brain.html`, `brain.css`, and `brain.js` with no build step.
- [x] Three synchronized views: **Graph** (relationships), **Timeline**
      (history), and **Library** (typed records and search).
- [x] Add a census rail (counts by kind and status), health signals, and
      ingest/consolidate actions.
- [x] Add a proposals inbox through the `proposed` lifecycle filter.
- [x] Add a detail panel with content, provenance, confidence, reinforcement,
      entities, related memories, and confirm/reject/pin/edit/forget actions.
- [x] Require a second click before forgetting a memory.
- [x] Add a composer for proposing or immediately remembering a new claim.
- [x] Link Galaxy and Brain so the two surfaces are one product.
- [x] Match the existing visual language: dark glass, Instrument Serif headings,
      monospace labels, kind-coloured glows.

### Phase 2B acceptance criteria

- The graph, timeline, and library show the same filtered memory set.
- Search narrows all three views and shows lexical scores.
- Selecting a memory highlights it everywhere and reveals its evidence.
- Confirm, reject, pin, edit, and forget round-trip through the API and refresh
  the surface.
- The page works without a model key because memory is local.
- The layout degrades to a single column on narrow screens.

---

## Phase 2C — Home hub and capture loop (complete)

Home is the surface that answers "what needs me right now?" and turns a
conversation into memory without a detour through the Brain.

- [x] Move the Galaxy to `/galaxy.html`; make `/` the Home command deck.
- [x] Add `GET /home`: one aggregate read with status, brief, Needs You
      (proposed memories plus pending approvals), recent confirmed memories,
      recent sessions, recent audit events, and memory stats.
- [x] Add `viewer/index.html`, `home.css`, and `home.js` with no build step:
      daily brief, Needs You queue with inline confirm/reject and approve/deny,
      quick capture, recent threads, recent memories, and activity.
- [x] Add a Ctrl+K command palette for ask, capture, Brain, Galaxy, ingest,
      consolidate, rebuild, and refresh.
- [x] Add `POST /memory/remember` to split text into atomic memories and confirm
      them immediately.
- [x] Add a conservative automatic proposal detector for durable first-person
      statements; questions and commands are ignored.
- [x] Handle an explicit "remember that …" inside chat on the server, without a
      model call.
- [x] Return `memories_proposed` on `/chat`, `/chat/stream`, and the
      orchestrator result so the UI can confirm what was captured.
- [x] Add **Remember this** to the Galaxy answer card, backed by
      `/memory/remember`.
- [x] Add `Database.recent_sessions()` for thread previews.
- [x] Keep audit payloads in `/home` light by omitting heavy `detail` fields.
- [x] Update Home/Galaxy/Brain navigation so the three surfaces read as one
      product.

### Phase 2C acceptance criteria

- Home loads without a model key and shows the brief, inbox, and recents.
- A quick capture can be proposed or confirmed, and the Needs You queue updates
  in place.
- Confirming or rejecting a proposal, and approving or denying an action, both
  work from Home and are audited.
- A first-person statement in chat creates a proposal; a question does not.
- "Remember that …" confirms immediately and works without a model call.
- **Remember this** on a long answer splits it into atomic confirmed memories.
- `/home` stays small enough to load quickly and omits heavy audit detail.
- Tests cover the new store methods, chat paths, and API endpoints.

---

## Phase 2C.1 — Galaxy load performance (complete)

The Galaxy was waiting on the network and on its own intro timers even when
everything was already local.

- [x] Self-host pinned `three.js` and `3d-force-graph` in `viewer/vendor/`
      instead of loading them from jsDelivr.
- [x] Load the fonts stylesheet without blocking first paint.
- [x] Preload and defer the vendor scripts.
- [x] Remove the `document.fonts.ready` wait from boot.
- [x] Shorten the camera intro and reveal the stage as soon as the first frame
      is ready.
- [x] Enable gzip for static assets and JSON; keep SSE uncompressed.
- [x] Stop sending `Cache-Control: no-store` for viewer assets. Vendor files
      are immutable and versioned; HTML/CSS/JS revalidate with a cheap 304.
- [x] Bump the version to `0.7.1`.

### Phase 2C.1 acceptance criteria

- A warm Galaxy load reveals the graph in about a second or less.
- Vendor scripts are served locally, compressed, and reused from cache.
- The loader no longer waits on fonts or arbitrary multi-second timers.
- Chat streaming still flushes token by token.

---

## Phase 2E.0 — Connector foundation (complete)

The first phase of M1. It defines the contract every external adapter
implements, with no credentials and no network, so 2E.1 and 2E.2 add adapters
rather than architecture.

- [x] Add `ashra/connectors/` with `Connector`, `Capabilities`, `Capability`,
  `Record`, `Source`, `Candidate`, `NormalisedRecord`, `PollResult`, and
  `ConnectorHealth`.
- [x] Enforce declared capabilities at the point of use: `read`, `draft`,
  `write`, `delete`, with `delete` impossible without `write`.
- [x] Add `ConnectorRegistry` with registration, lookup, capability checks,
  status, health, and credential-readiness reporting.
- [x] Add `Connector.run()`: capability guard, per-connector rate limit, and
  health recording for both success and failure.
- [x] Add `ConnectorRegistry.ingest()`: poll, normalise, persist Sources
  idempotently by `(connector, kind, external_id)`, and return candidates
  without writing tasks or memories.
- [x] Add `ashra/credentials.py`: reference validation, `MemoryVault` for
  tests, a Windows Credential Manager vault using `ctypes`, and redaction by
  reference.
- [x] Add `ashra/sensitivity.py` as the single privacy vocabulary
  (`public`, `internal`, `private`, `sensitive`, `secret`) with conservative
  classification and inheritance; memory now uses it.
- [x] Add schema v4: `connectors` and `sources` tables, plus a forward-only
  migration that maps legacy `normal` sensitivity to `internal`.
- [x] Add `ReplayConnector` so the contract is testable with fixtures and no
  mailbox.
- [x] Add `GET /connectors` and `GET /connectors/{id}`; include the connector
  count in `GET /status`.
- [x] Keep every response free of credential values; the API reports reference
  names and readiness only.
- [x] Test capability enforcement, idempotent ingestion, candidate inheritance,
  rate limiting, failure isolation, schema migration, credential redaction, and
  the API paths.
- [x] Bump the version to `0.8.0`.

### Phase 2E.0 acceptance criteria

- A read-only connector registers, polls, and produces Sources plus candidate
  proposals without any network access or credential.
- Polling twice does not duplicate a Source; the second pass updates it.
- A connector asked to exceed its declared capability fails with a named error.
- One broken connector reports `status: "error"` and does not block another.
- A candidate inherits the stronger of its own and its source's sensitivity.
- `GET /connectors` lists declarations and readiness without a secret value.
- The full suite passes with no credentials configured.

### Deliberately deferred

- **Approval payload hashing and nonces.** PRD 6.4 and scenario A5 require an
  immutable, single-use, hash-bound approval. Today's approval row binds the
  tool and arguments but has no payload hash. This belongs with the Desk and
  the approved-execution phase (2D/2G), not the connector read path.
- **Connector configuration parsing.** The `connectors` block in PRD Appendix B
  arrives with the first real adapter (2E.1), where its fields can be validated
  against a working implementation.
- **Runs table.** Turn-level run records are required by the Desk (2D) and are
  not needed to poll a read-only source.

---

## Phase 2 — The Voice (implemented in this pass)

- [x] Speak every answer with `speechSynthesis`, preferring a British English
      voice (`en-GB`) and falling back to any English voice.
- [x] Add the 🎙 mic button using `SpeechRecognition` /
      `webkitSpeechRecognition` (Chrome and Edge).
- [x] Route the final transcript through the same `askAshra()` path and
      `POST /chat`; no second answer path.
- [x] Show `● listening…`, `● thinking…`, `● speaking…`, and microphone error
      status lines.
- [x] Unlock audio on the first user interaction (`pointerdown` / `keydown`)
      with a silent utterance, because browsers block autoplay audio.
- [x] Cancel speech when a new question starts, when the mic opens, and when a
      request fails.
- [x] Disable the mic while a request is in flight.
- [x] Expose `speak`, `listen`, `stopSpeaking`, `voice`, `speechSupported`, and
      `voiceInputSupported` on `window.__ashra` for future automation.

Implementation note: voice remains a viewer concern. It calls the same
`/chat` endpoint and reuses `askAshra()`; the assistant does not know about it.

Test plan:

1. Open http://127.0.0.1:4700/ in Chrome or Edge.
2. Type a question and press Ask. The answer should appear and be spoken in a
   British English voice.
3. Click 🎙, allow the microphone, and speak
   "What is our pricing strategy?". The status should read `● listening…`
   while the transcript builds, then `● thinking…`, then the answer is shown
   and spoken.
4. Click 🎙 again while listening to stop early; any captured transcript is
   submitted, otherwise the hint returns.
5. Ask two questions in a row; the previous answer's speech should stop when
   the new question is submitted.
6. Deny microphone access and confirm the `● microphone blocked` status.
7. Check DevTools console with `window.__ashra.voice()` to see the chosen
   voice, and `window.__ashra.speechSupported` /
   `window.__ashra.voiceInputSupported` for capability flags.

---

## Phase 3 — The Magic (implemented in this pass)

- [x] Consume the `nodes` array returned by `/chat`.
- [x] Fly the camera to the top source node, highlight it and its neighbors,
      and open the note panel.
- [x] If 4+ notes were used, highlight the whole source cluster and fit the
      camera to its bounds instead of opening one note.
- [x] Make source labels clickable chips that fly to the corresponding note.
- [x] Clear a cluster highlight with Escape, the background, or Reset view.
- [x] On phones, fly and highlight without auto-opening the panel, so the
      answer card stays visible.
- [x] Keep the spoken answer flowing while the camera moves.
- [x] Reuse `focusNode()`, `clearFocus()`, and `startCameraFlight()`.

Implementation note: `askAshra()` still owns the single answer path. After a
successful `/chat`, it renders source chips and calls `revealSources(data.nodes)`;
voice and typed questions therefore behave identically.

---

## Phase 4 — The Personality (implemented in this pass)

- [x] Move the persona and grounding rules into `ashra/persona.py`.
- [x] Make the assistant a dry, polite British butler; use "sir" occasionally.
- [x] Ask for two sentences of fact plus one short, understated dry line,
      capped at three sentences / 65 words, without reciting the note back.
- [x] Handle small talk without retrieval or camera movement (`nodes: []`).
- [x] Add a boot greeting using the real graph count:
      `"Good evening, sir. 26 notes indexed, all present and accounted for."`
- [x] Keep the facts-only constraint from Phase 1; persona never overrides
      grounding.

Small-talk coverage includes greetings, how-are-you, identity, capabilities,
thanks, farewells, acknowledgements, and compliments. Only short whole-message
matches qualify, so "Hello, what is our pricing strategy?" still goes to
retrieval.

The greeting is spoken on the first `pointerdown` / `keydown`, after the silent
audio unlock. If the first interaction is the mic button, the greeting is
skipped so it cannot talk over the user.

Implementation note: `persona.py` separates tone (`PERSONA`) from grounding
(`GROUNDING_RULES`) so the character can change without weakening the
"answer only from notes" rule.

---

## Phase 5 — Total Recall (implemented in this pass)

- [x] Add `POST /remember` accepting a spoken/typed phrase (`{"text": "..."}`).
- [x] Write a real markdown note into `notes/captures/` with an H1 title
      derived from the first eight words.
- [x] Reload the note index and add the new node to the live galaxy.
- [x] Spawn the node beside its most related note, pulse it, and fly the camera
      to it.
- [x] Confirm out loud with one dry line, e.g.
      `Noted, sir. "The new decaf launch" is in the vault, filed beside Cash Flow Forecast.`
- [x] Keep the write path server-side and validate/sanitize titles and paths.
- [x] Keep `build.py` as the source of truth: `/remember` calls the same
      `discover_notes` / `build_graph` / `write_output` functions and then
      returns the full graph to the browser.

Command handling lives in `viewer/app.js`: "remember that …", "make a note
that …", "save a note that …", "note that …", and "jot down that …". A trailing
question mark or an interrogative body is treated as a question, so "remember
what our pricing strategy is?" still goes to `/chat`.

Implementation notes:

- `ashra/remember.py` owns validation, title/slug creation, related-note
  selection (`score_notes`), the `Related: [[Title]]` wikilink, the atomic
  rebuild, and the spoken confirmation.
- `build.graph_payload()` is shared by `write_output()` and `/remember`, so the
  API and a full rebuild cannot drift apart.
- Captures sort into a new `Captures` constellation; adding that folder changes
  numeric node ids, so the client replaces the whole graph in place and keeps
  every existing star at its previous position by `path`.
- Writes are serialised with a lock; the filename is a lowercase slug with only
  `[a-z0-9-]`, so a capture can never escape `notes/captures/`.
- Captures are plain notes, so the next `/chat` question retrieves them through
  the existing uncached note loader with no extra indexing step.

---

## Phase 6 — Your own assistant (future)

The prompt pack is the skeleton. The conversion to a personal assistant should
happen along these seams:

### Provider abstraction

Replace or extend `ashra/providers.py`:

- OpenAI / local model / custom HTTP backend
- streaming responses
- model hot-swap from the UI
- per-task model selection (cheap model for triage, strong model for reasoning)

### Retrieval abstraction

Replace `ashra/retrieval.py` with:

- embeddings + vector store
- hybrid keyword + semantic search
- recency and source-authority boosts
- per-folder scopes ("only look at finance notes")

The `/chat` response can keep `nodes` as the source contract.

### Memory

Replace `ashra/sessions.py` with:

- SQLite or JSON persistence
- long-term user preferences
- "what was I doing last Tuesday?" timeline
- explicit memory writes and deletion

### Tools / actions

Add a new `actions/` package and a tool-calling layer:

- drafts emails, researches, repurposes content
- calendar and task actions
- a "do it" confirmation gate before side effects
- allowlists, dry-run mode, and an audit log

Do not let the model execute arbitrary shell commands or file writes without a
confirmation and a scope check.

### Voice and presence

- cloned voice / TTS provider
- wake word ("A.S.H.R.A…")
- true barge-in
- optional HUD / reactor visualization
- desktop or mobile shell if the browser is no longer enough

### Security and privacy

Before exposing this beyond `127.0.0.1`:

- add authentication and CSRF protection
- serve over TLS
- keep secrets in the OS keychain or environment, not in a served file
- add per-session rate limits
- redact secrets from logs
- audit every write/tool action
- back up the notes vault

### Product direction

The current `notes/` vault is the content layer. The long-term assistant should
keep that layer portable: plain markdown in, plain markdown out. The graph,
retrieval index, and memory are derived views, not the source of truth.

---

## File map

```text
config.json                 private key + model settings (git-ignored)
config.example.json         safe placeholder template
.gitignore                  keeps secrets, caches, and logs out of git
pyproject.toml              package metadata + test configuration
LICENSE                     MIT license
server.py                   Uvicorn entrypoint + startup banner
build.py                    markdown -> viewer/graph-data.js
ashra/
  __init__.py               package exports
  api.py                    FastAPI routes, SSE, request IDs, error mapping
  config.py                 config validation
  credentials.py            credential refs, redaction, Windows vault
  sensitivity.py            the five privacy classes and classification
  db.py                     SQLite sessions, messages, approvals, sources, audit
  notes.py                  note loading
  retrieval.py              keyword scoring
  sessions.py               conversation-store protocol + in-memory store
  providers.py              provider transports
  persona.py                assistant tone + small talk
  recall.py                 deterministic capture-history answers
  remember.py               capture CRUD + graph rebuild
  tools.py                  permission tiers + tool registry + scoped filesystem
  agents/                   specialist registry, permission ceiling, child runner
  connectors/               adapter contract, registry, replay fixture
  integrations/             MCP client, integration config, tool registration
  mcp_server.py             optional read-only MCP filesystem server
  memory.py                 typed memory, lifecycle, retrieval, graph, health
  orchestrator.py           bounded tool loop + approval continuation
  chat.py                   answer + streaming pipeline
docs/prd/ashra-prd.pdf      v1.0 product requirements (authoritative scope)
data/                       local SQLite database (git-ignored)
viewer/
  index.html                galaxy + chat bar
  app.js                    graph, camera, chat UI
  styles.css                cinematic UI + chat dock
  graph-data.js             generated graph data
  brain.html                memory surface: graph, timeline, library
  brain.js                  memory views, search, detail, lifecycle actions
  brain.css                 brain layout and memory styling
notes/                      private markdown knowledge base
tests/                      unittest suite
.github/workflows/ci.yml    GitHub Actions test matrix
```

---

## How to run

```bash
cd path/to/A.S.H.R.A
python -m pip install -e ".[dev]"
python build.py          # after notes change
python server.py         # http://127.0.0.1:4700/
```

The interactive API docs are at `http://127.0.0.1:4700/api/docs`.

Then:

1. Open the URL in Chrome.
2. Keep your provider API key in `config.json` (currently configured for
   NVIDIA NIM).
3. Use a model your NVIDIA account can access. The current model id is
   `nvidia/nemotron-3.5-super-vl-120b-a12b`.
4. Reload the page and ask a question.

The server re-reads `config.json` per request, so a restart is not required for
key/model changes, but reloading the page is still the simplest way to reset
the client session.

---

## Example questions for the current notes

- "What is our pricing strategy for wholesale accounts?"
- "What should be on the opening checklist before the roastery opens?"
- "How are we handling hiring and training for the new barista role?"
