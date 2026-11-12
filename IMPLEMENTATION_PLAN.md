# JARVIS / Personal Assistant — Implementation Plan

## North star

Start from the Prompt Pack's 3D knowledge galaxy, add a note-grounded brain,
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

---

## Current baseline

**Prompt 1 — The Galaxy: complete.**

- `build.py` scans `notes/**/*.md` with the standard library and writes
  `viewer/graph-data.js`.
- 25 notes, 94 links, ~689-character average excerpts.
- Every node id equals its index in the nodes array.
- `viewer/index.html` + `app.js` render a cinematic 3D force graph from CDN
  libraries, with starfield, glow, color groups, idle drift, camera flights,
  neighbor highlighting, and a note panel.
- `server.py` serves only `viewer/` on `127.0.0.1:4700`.

**Prompt 2 — The Brain: implemented in this pass.**

- `config.json` in the project root (gitignored; the key stays server-side).
- Current configuration targets NVIDIA NIM with
  `nvidia/nemotron-3.5-super-vl-120b-a12b`; Anthropic remains supported.
- Server-side note loading, keyword retrieval, session history, model API
  call, and `POST /chat`.
- Viewer input bar with thinking status, answer card, and source labels.
- No API key is ever sent to the browser or placed in a served file.

---

## Architecture principles

1. **Keep secrets server-side.** `config.json` lives outside `viewer/`; the
   viewer server is rooted at `viewer/`. The server also explicitly refuses
   requests for `config.json` and `.env`.
2. **Keep the brain modular.** Retrieval, sessions, provider transport, and
   configuration are separate modules. A future personal assistant can replace
   one without touching the others.
3. **Keep the browser contract stable.** The viewer only knows
   `POST /chat -> {answer, nodes, sources, session_id}`. New backends, tools, or
   models should preserve that shape where possible.
4. **Prefer stdlib until a dependency earns its place.** The current brain uses
   only `urllib` and the standard library.
5. **Ground answers in notes.** The system prompt forbids outside knowledge and
   requires the assistant to admit when the notes do not cover a question.

---

## Architecture

```text
viewer/ (public)
  index.html
  app.js  ── POST /chat ──┐
  styles.css              │
                          ▼
server.py (local HTTP) ── brain/chat.py
                             ├── notes.py      load + clean markdown
                             ├── retrieval.py  keyword scoring -> top 6
                             ├── sessions.py   short in-memory history
                             ├── providers.py  Anthropic / NVIDIA NIM (swap target)
                             └── config.py     config.json validation
                          │
                          ▼
                 model provider (Anthropic or NVIDIA NIM)
```

`config.json` and `notes/` stay outside the served tree.

---

## Phase 1 — The Brain (current)

### 1. Configuration

- [x] Create root `config.json` with `api_key`, `model`, `provider`, and tuning
      fields.
- [x] Validate and bound config values in `brain/config.py`.
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
- [ ] Future: add more backends by extending `brain/providers.py`.

### 5. Sessions

- [x] Thread-safe in-memory `SessionStore`.
- [x] Client sends a `session_id`; server creates/validates one.
- [x] Keep the last 8 messages by default (about 4 turns).
- [x] Include history in the provider message list so follow-ups work.
- [ ] Future: replace with persistent memory (SQLite/JSON) behind the same
      `ensure`, `history`, and `append_exchange` methods.

### 6. `POST /chat`

- [x] Accept `{question, session_id?}`.
- [x] Validate JSON, content length, question type, and session id type.
- [x] Return `{answer, nodes, sources, session_id, model}`.
- [x] Map errors to `400` / `500` / `502` / `503` with JSON details.
- [x] Do not log request bodies or secrets.
- [x] Keep `GET` static serving for the viewer.

### 7. Viewer chat UI

- [x] Add a bottom input bar and Ask button.
- [x] Show a thinking status, answer card, and source labels.
- [x] Keep the session id in `sessionStorage`.
- [x] Expose `window.__galaxy.ask(question)` for future voice and automation.
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

- `python server.py` starts on port 4700 and prints the brain status.
- The viewer loads and the input bar is usable.
- With a valid key in `config.json`, a question returns a 2–3 sentence answer
  grounded in the notes, plus the source node indexes.
- With the placeholder key, the UI says the brain is not configured instead of
  failing silently.
- Follow-up questions in the same tab use the short server-side history.
- No secret appears in browser DevTools, HTML, or a served file.

---

## Phase 2 — The Voice (implemented in this pass)

- [x] Speak every answer with `speechSynthesis`, preferring a British English
      voice (`en-GB`) and falling back to any English voice.
- [x] Add the 🎙 mic button using `SpeechRecognition` /
      `webkitSpeechRecognition` (Chrome and Edge).
- [x] Route the final transcript through the same `askJarvis()` path and
      `POST /chat`; no second answer path.
- [x] Show `● listening…`, `● thinking…`, `● speaking…`, and microphone error
      status lines.
- [x] Unlock audio on the first user interaction (`pointerdown` / `keydown`)
      with a silent utterance, because browsers block autoplay audio.
- [x] Cancel speech when a new question starts, when the mic opens, and when a
      request fails.
- [x] Disable the mic while a request is in flight.
- [x] Expose `speak`, `listen`, `stopSpeaking`, `voice`, `speechSupported`, and
      `voiceInputSupported` on `window.__galaxy` for future automation.

Implementation note: voice remains a viewer concern. It calls the same
`/chat` endpoint and reuses `askJarvis()`; the brain does not know about it.

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
7. Check DevTools console with `window.__galaxy.voice()` to see the chosen
   voice, and `window.__galaxy.speechSupported` /
   `window.__galaxy.voiceInputSupported` for capability flags.

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

Implementation note: `askJarvis()` still owns the single answer path. After a
successful `/chat`, it renders source chips and calls `revealSources(data.nodes)`;
voice and typed questions therefore behave identically.

---

## Phase 4 — The Personality (implemented in this pass)

- [x] Move the persona and grounding rules into `brain/persona.py`.
- [x] Make the assistant a dry, polite British butler; use "sir" occasionally.
- [x] Ask for two sentences of fact plus one short, understated dry line,
      capped at three sentences / 65 words, without reciting the note back.
- [x] Handle small talk without retrieval or camera movement (`nodes: []`).
- [x] Add a boot greeting using the real graph count:
      `"Good evening, sir. 25 notes indexed, all present and accounted for."`
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

- `brain/remember.py` owns validation, title/slug creation, related-note
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

Replace or extend `brain/providers.py`:

- OpenAI / local model / custom HTTP backend
- streaming responses
- model hot-swap from the UI
- per-task model selection (cheap model for triage, strong model for reasoning)

### Retrieval abstraction

Replace `brain/retrieval.py` with:

- embeddings + vector store
- hybrid keyword + semantic search
- recency and source-authority boosts
- per-folder scopes ("only look at finance notes")

The `/chat` response can keep `nodes` as the source contract.

### Memory

Replace `brain/sessions.py` with:

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
- wake word ("Jarvis…")
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
server.py                   static viewer + POST /chat
build.py                    markdown -> viewer/graph-data.js
brain/
  __init__.py               package exports
  config.py                 config validation
  notes.py                  note loading
  retrieval.py              keyword scoring
  sessions.py               short conversation memory
  providers.py              Anthropic provider + swap seam
  chat.py                   answer pipeline + system prompt
viewer/
  index.html                galaxy + chat bar
  app.js                    graph, camera, chat UI
  styles.css                cinematic UI + chat dock
  graph-data.js             generated graph data
notes/                      private markdown knowledge base
```

---

## How to run

```bash
cd "C:\Accumulated Work\4. Projects\myjarvis"
python build.py          # after notes change
python server.py         # http://127.0.0.1:4700/
```

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
