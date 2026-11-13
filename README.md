# A.S.H.R.A

A lightweight, local-host chatbot and note vault built on top of an LLM. It exposes a simple UI in `viewer/`, lets you store and query Markdown notes, and answers questions by searching those notes with an LLM.

Designed for personal or small-team use - no public endpoints, no external storage, only your machine.

## Installation

```bash
git clone https://github.com/LohithP07/A.S.H.R.A.git
cd A.S.H.R.A

# Optional: create a virtual environment
python -m venv .venv
.\.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # macOS/Linux
```

The project relies only on the Python standard library.

## Configuration

The LLM provider is configured in `config.json`:

```json
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "key": "YOUR_API_KEY"
}
```

Copy `config.example.json` to `config.json` and fill in your key. Never commit secrets - `.gitignore` already skips it.

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

Note filenames use this pattern:

```text
<timestamp-ms> - <slug-title> - <type>.md
```

For example: `1699450960000 - summer-menu - product.md`. This keeps notes sortable and gives the UI a friendly title.

## Features

- Chat with your notes: `POST /chat` builds a graph of related notes and asks the LLM.
- Automatic graph generation: `build.py` produces `viewer/graph-data.js` from all notes.
- Note CRUD through `/remember`, `/append`, `/rename`, and `/forget`.
- Secure by design: local-only server, no open endpoints.
- Configurable provider: swap between DeepSeek, OpenAI, Anthropic, and others via `config.json`.

## Development

| Task | Command |
|------|---------|
| Start the server | `python server.py` |
| Rebuild the viewer index | `python build.py` |
| Run tests (if added) | `pytest` |
| Lint / format | `flake8` and `black` |

To add a new note category:

1. Create a Markdown file under the appropriate folder, e.g. `notes/marketing/new-plan.md`.
2. Run `python build.py` to rebuild the graph index.

## Under the hood

- `server.py` - HTTP server with custom routing, built on `http.server`.
- `brain/chat.py` - `ChatService.ask()` parses the query, retrieves related notes, builds a graph, and calls the LLM.
- `brain/remember.py` - captures, appends, renames, and deletes note files.
- `brain/config.py` - loads and validates `config.json`.
- `viewer/` - static front-end assets.

## Contributing

1. Fork the repository.
2. Create a branch.
3. Add your changes and tests.
4. Submit a pull request.

All contributions should pass the current tests and style checks.

## License

MIT (c) 2026 A.S.H.R.A Project.
