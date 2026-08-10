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
openbrain backup --status          # whether last night's backup worked
```

Unit files are tracked in `deploy/systemd/` and installed by symlink into
`~/.config/systemd/user/`.

Logs are whatever journald keeps, which on this laptop is volatile and often
nothing — deliberately not worked around. `openbrain backup --status` is the
health signal instead: it reads the manifests on disk, so it survives a
reboot and reports `last_failure` with the archive, time, and reason.

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


## Retrieval

Search fuses PostgreSQL full-text with pgvector similarity using Reciprocal
Rank Fusion, then adjusts the order with bounded secondary signals.

**Query input is literal natural language.** There is no operator syntax:
`-foo` does not negate, quotes do not force a phrase, and `|` is not an OR.
`plainto_tsquery` discards those characters before OpenBrain sees them, so
they are matched as ordinary words.

Both arms use the same RRF constant, so either can reach the top. Earlier
versions used k=1 for text and k=60 for vectors, which capped the best
semantic match below the twentieth-best keyword match - vector similarity
could only break ties among rows the keyword arm had already found.

Ordering also accounts for recency, importance and usage:

```
score = normalized_relevance
      + 0.05 * recency        (exponential decay, 180-day half-life)
      + 0.03 * importance
      + 0.02 * access         (explicit fetches, capped at 10)
```

The three secondary weights are capped at 0.10 in total and the bound is
enforced at startup, so they reorder rows of similar relevance without ever
overriding it. Tune them under `ranking:` in `config/settings.yaml`.

```bash
openbrain search "how are backups verified" --explain
```

`--explain` prints the per-result breakdown - which branch ran, the raw and
normalized relevance, and each signal's contribution - so a surprising result
can be diagnosed instead of guessed at.

## Tags

Tags carry provenance: `user`, `keyword`, `pattern`, `entity`, `source`. Only
`user` tags are protected, and tags with missing provenance are treated as
protected too, since rows predating provenance tracking cannot be
distinguished from deliberate ones.

```bash
openbrain retag --dry-run            # what would change
openbrain retag                      # rewrite, needs a fresh verified backup
openbrain retag --undo FILE --dry-run
openbrain retag --undo FILE          # revert
```

`retag` refuses to run without a verified backup of *this* database from the
last 24 hours, writes an undo file before touching a row, and updates each row
under compare-and-set so a concurrently edited memory is skipped rather than
clobbered. `--undo` is exempt from the backup gate, because it must work in
exactly the emergency where the backup is stale.


## Auto-Capture

A Claude Code `Stop` hook records that a session has new turns; a timer drains
the queue every 15 minutes, asks a local model for durable facts, and stores
the survivors as `source="claude-code-auto"`.

```bash
openbrain capture --status
openbrain capture --scan-recent 5      # extract from recent transcripts, write nothing
openbrain capture --drain --dry-run    # show candidates for queued sessions
openbrain capture --install-hook ~/.claude/settings.json
openbrain capture --sync-hook-config   # after changing capture settings
```

**Opt-in twice.** `capture.enabled` must be true *and* the session's directory
must appear in `capture.projects`. An empty allowlist captures nothing. The
hook reads a small JSON artifact (`~/.local/state/openbrain/capture-hook.json`)
because it is stdlib-only and cannot parse YAML — regenerate it with
`--sync-hook-config` after any settings change.

**What gets captured.** Decisions and why, gotchas and their cause, stated
preferences, resolved root causes. A fact is stored only if a quote supporting
it appears **verbatim** in the transcript the model was shown; the quote itself
is then discarded. Most sessions produce nothing, and that is the intended
outcome — the bar is deliberately set so silence is cheaper than noise.

**Secrets.** Credential-shaped and high-entropy strings are redacted before
anything reaches the model, and candidates are re-scanned before storage. The
endpoint is pinned to loopback. Bulk removal covers the **live database only**:

```bash
# via MCP: memory_bulk_delete with sources=["claude-code-auto"], confirm=true
```

It does not touch retained `pg_dump` archives, WAL, or dead tuples, so a
credential that reached the store must be rotated regardless of deletion.

**After restoring the database**, capture progress on disk will be ahead of it
and the worker refuses to drain rather than silently skipping the rows the
restore lost:

```bash
systemctl --user stop openbrain-capture.timer
# restore the database
openbrain capture --reconcile
systemctl --user start openbrain-capture.timer
```

`openbrain capture --init-state` initializes the generation checkpoint on a
clean install; it refuses if any capture state or any `claude-code-auto` row
already exists, and points at `--reconcile` instead.

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
openbrain get <memory-id>       # fetch one memory; counts as an access
openbrain migrate               # apply additive schema changes
openbrain retag --dry-run       # preview tag cleanup
openbrain capture --status      # auto-capture state
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

Tests marked `integration` are deselected by default and run against a
**separate `openbrain_test` database**, never the live corpus:

```bash
sudo -u postgres createdb -p 5433 -O openbrain openbrain_test
sudo -u postgres psql -p 5433 -d openbrain_test \
  -c 'CREATE EXTENSION "uuid-ossp"' -c 'CREATE EXTENSION vector'
psql -h 127.0.0.1 -p 5433 -U openbrain -d openbrain_test -f src/db/schema.sql
DB_NAME=openbrain_test .venv/bin/openbrain migrate

.venv/bin/python -m pytest -m integration
```

The superuser commands use `-p 5433` without `-h`: adding `-h` forces TCP,
where `sudo -u postgres` supplies no credential, and omitting `-p` targets a
different cluster on 5432. The fixture fails rather than skips if the database
is missing, and refuses to run if it resolves to the production database.

Useful checks:

```bash
git diff --check
```

## Notes

- `.env`, virtualenvs, Python caches, build output, and Node dependencies are ignored.
- The committed web app is regular source under `web/`; it is not a nested Git repository.
- If embeddings fail, memories are still stored and can be found by text search.
