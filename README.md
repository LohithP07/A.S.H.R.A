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

The Python backend relies only on the standard library. The viewer loads Three.js and 3d-force-graph from a CDN.

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
| `/chat` | POST | `{ "question": "...", "session_id": "optional" }` | Ask the LLM; returns answer and note graph |
| `/remember` | POST | `{ "text": "...", "source": "typed" }` | Create a new note |
| `/append` | POST | `{ "text": "...", "last": true, "path": "note.md" }` | Append to an existing note |
| `/rename` | POST | `{ "title": "New title", "last": true, "path": "note.md" }` | Rename a note |
| `/forget` | POST | `{ "last": true, "path": "note.md", "confirm": true }` | Delete a note - requires confirmation |

All responses are JSON. The server only serves files inside `viewer/`; requests for `config.json`, `.env`, or `.git` are blocked.

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
- Automatic graph generation: `build.py` produces `viewer/graph-data.js` from all notes.
- Note CRUD through `/remember`, `/append`, `/rename`, and `/forget`.
- Secure by design: local-only server, no open endpoints.
- Configurable provider: Anthropic, NVIDIA NIM, and any OpenAI-compatible endpoint via `config.json`.

## Development

| Task | Command |
|------|---------|
| Start the server | `python server.py` |
| Rebuild the viewer index | `python build.py` |
| Run tests | `python -m unittest discover -v` |
| Compile check | `python -m compileall -q ashra build.py server.py` |

To add a new note category:

1. Create a Markdown file under the appropriate folder, e.g. `notes/marketing/new-plan.md`.
2. Run `python build.py` to rebuild the graph index.

## Under the hood

- `server.py` - HTTP server with custom routing, built on `http.server`.
- `ashra/chat.py` - `ChatService.ask()` parses the query, retrieves related notes, builds a graph, and calls the LLM.
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
