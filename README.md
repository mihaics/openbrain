# OpenBrain

Personal semantic memory system with three access paths:

- MCP tools for agents and editors
- REST API for applications
- Next.js web UI for browsing, searching, editing, and graphing memories

OpenBrain stores memories in PostgreSQL with pgvector embeddings, tags, extracted entities, metadata, and source labels. Search combines PostgreSQL full text with vector similarity when embeddings are available.

## Current Stack

| Layer | Technology |
| --- | --- |
| Database | PostgreSQL 18 + pgvector, port 5433 |
| API | FastAPI |
| MCP | Python MCP server over stdio (low-level `mcp.server.Server`) |
| Web | Next.js 16, React 19, Base UI, Cytoscape |
| CLI | Python argparse entrypoint |
| Embeddings | Ollama, OpenAI, OpenRouter, or custom OpenAI-compatible API |

## Repository Layout

```text
.
├── config/settings.yaml        # Local default configuration
├── deploy/systemd/             # Tracked unit files, installed by symlink
├── src/
│   ├── main.py                 # MCP stdio server
│   ├── api/main.py             # FastAPI app
│   ├── cli/                    # openbrain CLI commands
│   ├── db/                     # schema, connection, query layer
│   ├── embedder/               # embedding providers
│   ├── extractors/             # entities + auto-tagging
│   ├── analytics/              # trends and weekly report generation
│   └── connectors/             # importers for external sources
├── tests/                      # pytest suite
└── web/                        # Next.js app
```

Use `web/` for the UI.

## How This Runs

Two systemd --user services, plus the system PostgreSQL cluster:

| Component | Detail |
| --- | --- |
| API | `openbrain-api.service` → uvicorn on 127.0.0.1:8000 |
| Web | `openbrain-web.service` → Next.js on :3777 |
| Database | System PostgreSQL 18, port **5433**, database/user `openbrain` |
| Embeddings | Ollama `qwen3-embedding:0.6b`, 1024 dimensions |
| Backups | `openbrain-backup.timer`, nightly 02:30, to `~/backups/openbrain/` |

```bash
systemctl --user status openbrain-api openbrain-web openbrain-backup.timer
systemctl --user restart openbrain-api
tail -n 50 ~/.local/state/openbrain/backup.log
```

Unit files are tracked in `deploy/systemd/` and installed by symlink into
`~/.config/systemd/user/`; the tmpfiles rule that creates the log directory
is `deploy/tmpfiles/openbrain.conf`, symlinked into `~/.config/user-tmpfiles.d/`.

**Why the backup logs to a file, not the journal.** On this machine journald
is running on the runtime journal only — `/var/log/journal` has not been
written since 2026-01-28 and no user journal is live — so
`journalctl --user -u <unit>` returns nothing for any user service, and a
failing nightly backup would leave no trace. The unit redirects stdout and
stderr to `~/.local/state/openbrain/backup.log` instead. If persistent
journaling is ever repaired system-wide, that redirect can go away.

MCP is not exposed over HTTP. Run the MCP server as a stdio process from your
MCP client.

## MCP Registration

The MCP server is registered in six agent configurations:

`~/.claude/.claude.json`, `~/.claude-quarticle/.claude.json`,
`~/.codex/config.toml`, `~/.gemini/settings.json`,
`~/.config/opencode/opencode.json`, `~/.config/harness/mcp.json`

## Backups

```bash
openbrain backup            # dump, verify, prune (what the timer runs)
openbrain backup --status   # age of newest verified dump; exit 1 if stale
openbrain backup --status --json
openbrain backup --verify-only
```

Dumps are `pg_dump -Fc` archives in `~/backups/openbrain/`, named
`openbrain-<seq>-<utc>.dump` with a sidecar `.json` manifest recording the
row count, embedding dimension, extension versions, size, and SHA-256.
Retention keeps the newest 60.

**What verification does and does not prove.** Every dump's TOC is checked
against the expected object set and its entire payload is decoded, which
catches truncation, bit-rot, and a broken `pg_dump`. It does **not** prove
the archive restores: nothing exercises `CREATE EXTENSION`, schema
creation, index rebuilds, or the `vector(1024)` column. Restore
verification was deliberately left out to avoid a superuser step.

### Restoring — untested procedure

> These commands have never been run on this machine. Restoring requires
> creating a database and the `uuid-ossp` and `vector` extensions, which
> needs superuser. Expect to debug this the first time.

```bash
sudo -u postgres createdb -O openbrain openbrain_restored
sudo -u postgres psql -d openbrain_restored -c 'CREATE EXTENSION "uuid-ossp"'
sudo -u postgres psql -d openbrain_restored -c 'CREATE EXTENSION vector'
PGPASSWORD=... pg_restore -h localhost -p 5433 -U openbrain \
  -d openbrain_restored --no-owner --no-privileges \
  ~/backups/openbrain/openbrain-<seq>-<utc>.dump
```

Check the restored row count against the manifest's `row_count`.

For embedding-level damage specifically, `openbrain reembed` repairs rows
in place and needs no restore at all.

## Local Development

Python setup:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Run the API locally:

```bash
openbrain serve --host 0.0.0.0 --port 8000 --reload
```

Run the web app locally:

```bash
cd web
npm install
npm run dev
```

The web app defaults to `http://localhost:8000` for the API. Override with:

```bash
NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

## MCP Usage

The MCP server is `src/main.py` and uses stdio:

```bash
python -m src.main
```

Example MCP client config:

```json
{
  "mcpServers": {
    "openbrain": {
      "command": "python",
      "args": ["-m", "src.main"],
      "cwd": "/path/to/open-brain"
    }
  }
}
```

Primary MCP tools include:

| Tool | Purpose |
| --- | --- |
| `memory_store` | Store a memory with embedding, entities, and tags |
| `memory_search` | Hybrid full-text/vector search |
| `memory_get` | Fetch one memory by ID |
| `memory_update` | Update content, source, tags, or importance |
| `memory_update_tags` | Replace tags while preserving tag provenance where possible |
| `memory_delete` | Delete one memory |
| `memory_bulk_delete` | Delete by filters, requires `confirm: true` |
| `memory_get_related` | Find semantically related memories |
| `memory_find_duplicates` | Find near-duplicate memory pairs |
| `memory_get_entity` | Find memories containing an entity |
| `memory_timeline` | Group memories by day |
| `memory_graph` | Entity co-occurrence graph |
| `memory_stats` | Aggregate memory stats |
| `memory_trends` | Trending tags |
| `memory_weekly_report` | Markdown activity report |

Example tool call:

```json
{
  "name": "memory_search",
  "arguments": {
    "query": "embedding dimension mismatch",
    "limit": 10,
    "include_score": true,
    "content_mode": "snippet"
  }
}
```

## REST API

Common endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `GET` | `/memories` | Recent memories |
| `POST` | `/memories` | Store a memory |
| `GET` | `/memories/{id}` | Fetch a memory |
| `PUT` | `/memories/{id}` | Update a memory |
| `DELETE` | `/memories/{id}` | Delete a memory |
| `POST` | `/memories/search` | Search memories |
| `GET` | `/memories/timeline` | Timeline data |
| `GET` | `/memories/graph` | Entity graph data |
| `GET` | `/entities/types` | Entity type counts |
| `GET` | `/entities/names` | Entity names by type |
| `GET` | `/stats` | Memory stats |
| `GET` | `/trends` | Trending topics |
| `GET` | `/report/weekly` | Weekly report |

Examples:

```bash
curl -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -d '{"content":"Ship README cleanup", "source":"manual", "tags":["docs"]}'
```

```bash
curl -X POST http://localhost:8000/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query":"README cleanup", "limit":5}'
```

## CLI

Install the package in editable mode first:

```bash
pip install -e .
```

Commands:

```bash
openbrain store "Remember this" --source cli --tag note
openbrain search "remember this" --limit 5
openbrain stats
openbrain report --days 7
openbrain import claude_code ~/.claude/sessions --limit 100
openbrain serve --reload
openbrain exec "ls -la"
openbrain exec --sandbox --timeout 120 "python -V"
openbrain reembed --dry-run
openbrain backup --status
```

## Configuration

Main config lives in `config/settings.yaml`. `.env` is for local secrets and runtime overrides and is intentionally ignored by Git.

Important environment variables:

| Variable | Purpose |
| --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Database connection |
| `EMBEDDER_PROVIDER` | `ollama`, `openai`, `openrouter`, or `custom` |
| `EMBEDDER_MODEL` | Embedding model name |
| `OLLAMA_BASE_URL` | Ollama endpoint |
| `OPENAI_API_KEY` | OpenAI embeddings |
| `OPENROUTER_API_KEY` | OpenRouter embeddings |
| `CUSTOM_API_URL`, `CUSTOM_API_KEY` | Custom OpenAI-compatible embeddings |
| `API_PORT` | API port, default `8000` |
| `WEB_PORT` | Web port, default `3777` |

## Database

Schema is in `src/db/schema.sql`.

The `memory` table stores:

- `content` and optional `raw_content`
- vector `embedding`
- JSONB `entities`, JSONB `metadata`, JSONB `tag_sources`
- text array `tags`
- `source`, `source_id`, `importance`, timestamps, and language

Indexes include pgvector HNSW for embeddings, GIN for tags/entities, and full-text search over content.

## Testing

```bash
.venv/bin/python -m pytest -q
```

Tests marked `integration` need the live cluster and are deselected by
default; run them with `-m integration`.

Useful checks:

```bash
git diff --check
```

## Notes

- `.env`, virtualenvs, Python caches, build output, and Node dependencies are ignored.
- The committed web app is regular source under `web/`; it is not a nested Git repository.
- If embeddings fail, memories are still stored and can be found by text search.
