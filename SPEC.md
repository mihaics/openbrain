# Open Brain - Architecture Specification

## Project Overview

- **Name**: Open Brain
- **Type**: Personal semantic memory system with MCP interface
- **Core**: PostgreSQL + pgvector for storage, FastMCP for MCP server, Python for ingestion
- **Goal**: Universal memory storage and retrieval accessible by any tool via MCP

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Any Tool (MCP Client)                  │
│   (Claude, Codex, OpenClaw, Custom Scripts, etc.)          │
└─────────────────────────┬───────────────────────────────────┘
                          │ MCP Protocol
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                      MCP Server (FastMCP)                   │
│   - memory_search     - memory_store                        │
│   - memory_get_related - memory_get_entity                   │
│   - memory_today     - memory_stats                         │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│                   Python Application                         │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐    │
│  │ Extractors   │ │ Tagging      │ │ Analytics        │    │
│  │ (Entities)   │ │ (Auto-tag)   │ │ (Trends)         │    │
│  └──────────────┘ └──────────────┘ └──────────────────┘    │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│                   PostgreSQL + pgvector                      │
│   - memory table with embeddings                            │
│   - GIN indexes for tags, entities                          │
│   - IVFFlat index for vector similarity                     │
└─────────────────────────────────────────────────────────────┘
```

---

## File Structure

```
open brain/
├── SPEC.md                 # This file
├── requirements.txt        # Python dependencies
├── config/
│   └── settings.yaml       # Configuration
├── src/
│   ├── __init__.py
│   ├── main.py             # MCP server entry point
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.sql      # Database schema
│   │   ├── connection.py  # DB connection
│   │   └── queries.py     # SQL queries
│   ├── extractors/
│   │   ├── __init__.py
│   │   ├── entities.py     # NER extraction
│   │   └── tagger.py      # Auto-tagging
│   ├── embedder/
│   │   ├── __init__.py
│   │   └── Ollama embedder
│   ├── analytics/
│   │   ├── __init__.py
│   │   ├── trends.py
│   │   └── weekly_report.py
│   └── ingestion/
│       ├── __init__.py
│       └── importer.py     # Bulk import
├── tests/
│   └── test_core.py
└── scripts/
    ├── setup_db.py         # Initialize database
    └── import_sample.py    # Sample data
```

---

## Database Schema

```sql
-- Core memory table
CREATE TABLE memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source VARCHAR(50) NOT NULL,
    source_id VARCHAR(255),
    content TEXT NOT NULL,
    raw_content TEXT,
    embedding vector(1536),
    entities JSONB DEFAULT '{}',
    tags TEXT[] DEFAULT '{}',
    tag_sources JSONB DEFAULT '{}',
    importance FLOAT DEFAULT 0.5,
    created_at TIMESTAMP DEFAULT NOW(),
    original_date TIMESTAMP,
    language VARCHAR(10),
    metadata JSONB DEFAULT '{}'
);

-- Indexes
CREATE INDEX idx_memory_embedding ON memory USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_memory_entities ON memory USING gin (entities);
CREATE INDEX idx_memory_tags ON memory USING gin (tags);
CREATE INDEX idx_memory_source ON memory (source);
CREATE INDEX idx_memory_created ON memory (created_at DESC);
```

---

## MCP Tools

| Tool | Parameters | Returns |
|------|------------|---------|
| `memory_search` | query, limit=5, sources, tags, date_from, date_to | List[Memory] with scores |
| `memory_store` | content, source, tags, metadata, importance | {id, status} |
| `memory_get_related` | memory_id, limit=5 | List[Memory] |
| `memory_get_entity` | entity_type, entity_name | List[Memory] |
| `memory_today` | limit=10 | List[Memory] |
| `memory_stats` | - | {total, by_source, top_tags, this_week} |

---

## Configuration (settings.yaml)

```yaml
database:
  host: localhost
  port: 5432
  name: openbrain
  user: postgres
  password: ${DB_PASSWORD}

embedder:
  provider: ollama  # or openai
  model: nomic-embed-text
  dimensions: 768

mcp:
  host: 0.0.0.0
  port: 8080

tags:
  deny_list:
    - password
    - secret
    - api_key
  default_tags:
    - auto

analytics:
  trend_weeks: 4
  weekly_report_day: 0  # Sunday
```

---

## Implementation Priority

1. **Database setup** - Schema + connection
2. **Embedder** - Ollama integration
3. **MCP server** - All tools working
4. **Entity extraction** - Basic NER
5. **Auto-tagging** - Keyword + pattern layers
6. **Trends** - Basic frequency analysis
7. **Weekly reports** - Markdown generation

---

## Acceptance Criteria

- [ ] MCP server starts and accepts connections
- [ ] Can store a memory and retrieve it via search
- [ ] Semantic search returns relevant results
- [ ] Entity extraction works on sample content
- [ ] Auto-tagging adds relevant tags
- [ ] Trends show top tags and changes
- [ ] Weekly report generates valid markdown
