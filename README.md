# OpenBrain

Personal semantic memory system with three access paths:

- MCP tools for agents and editors
- REST API for applications
- Next.js web UI for browsing, searching, editing, and graphing memories

OpenBrain stores memories in PostgreSQL with pgvector embeddings, tags, extracted entities, metadata, and source labels. Search combines PostgreSQL full text with vector similarity when embeddings are available.

## Current Stack

| Layer | Technology |
| --- | --- |
| Database | PostgreSQL 16 + pgvector |
| API | FastAPI |
| MCP | Python MCP server over stdio |
| Web | Next.js 16, React 19, Base UI, Cytoscape |
| CLI | Python argparse entrypoint |
| Embeddings | Ollama, OpenAI, OpenRouter, or custom OpenAI-compatible API |

## Repository Layout

```text
.
├── config/settings.yaml        # Local default configuration
├── docker-compose.yml          # postgres + api + web
├── src/
│   ├── main.py                 # MCP stdio server
│   ├── api/main.py             # FastAPI app
│   ├── cli/                    # openbrain CLI commands
│   ├── db/                     # schema, connection, query layer
│   ├── embedder/               # embedding providers
│   ├── extractors/             # entities + auto-tagging
│   ├── analytics/              # trends and weekly report generation
│   └── connectors/             # importers for external sources
├── tests/test_core.py
└── web/                        # Next.js app
```

Use `web/` for the UI.

## Quick Start

1. Create local environment config:

```bash
cp .env.example .env
```

2. Edit `.env`.

For local Ollama:

```env
EMBEDDER_PROVIDER=ollama
EMBEDDER_MODEL=nomic-embed-text
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

For OpenAI/OpenRouter, set the provider/model and API key instead.

3. Start the stack:

```bash
docker compose up -d
```

4. Open services:

| Service | URL |
| --- | --- |
| Web UI | `http://localhost:3777` |
| API | `http://localhost:8000` |
| API docs | `http://localhost:8000/docs` |
| Postgres host port | `localhost:5433` |

Docker Compose does not expose MCP as an HTTP service. Run the MCP server as a stdio process from your MCP client.

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

Current expected result:

```text
43 passed
```

Useful checks:

```bash
docker compose config
git diff --check
```

## Notes

- `.env`, virtualenvs, Python caches, build output, and Node dependencies are ignored.
- The committed web app is regular source under `web/`; it is not a nested Git repository.
- If embeddings fail, memories are still stored and can be found by text search.
