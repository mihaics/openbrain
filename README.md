# 🧠 Open Brain

> Personal semantic memory system with MCP interface. Store, search, and analyze everything that matters to you.

[![GitHub Stars](https://img.shields.io/github/stars/benclawbot/open-brain)](https://github.com/benclawbot/open-brain/stargazers)
[![Docker](https://img.shields.io/docker/pulls/benclawbot/open-brain)](https://hub.docker.com/r/benclawbot/open-brain)
[![License](https://img.shields.io/github/license/benclawbot/open-brain)](https://github.com/benclawbot/open-brain/blob/master/LICENSE)

## What is Open Brain?

Open Brain is a **personal knowledge management system** that acts as your second brain. It:

- 📥 **Ingests** data from anywhere (Telegram, WhatsApp, Claude Code, Gmail, files)
- 🧠 **Embeds** everything semantically (OpenRouter, OpenAI, Ollama, or any custom API)
- 🔍 **Searches** instantly using vector similarity
- 📊 **Analyzes** trends, clusters, and connections
- 🔔 **Notifies** you of important changes
- 🌐 **Serves** via MCP, REST API, CLI, or Dashboard

Think of it as **Obsidian meets ChatGPT memory** — but accessible from any tool.

---

## ✨ Features

### Core
- **Semantic Search** — Find memories by meaning, not just keywords
- **Auto-Tagging** — Automatic topic and entity extraction
- **Entity Recognition** — Extracts people, places, organizations, dates
- **Trend Analysis** — See what topics are emerging or declining

### Integrations
- **MCP Server** — Use from Claude, Codex, or any MCP client
- **REST API** — HTTP access for any application
- **CLI** — Command-line interface for quick operations
- **Source Connectors** — Import from Telegram, WhatsApp, Gmail, Claude Code

### UI
- **Next.js Web App** — Visualize memories, stats, and trends
- **Weekly Reports** — Automated markdown reports

---

## 🚀 Quick Start

### Prerequisites

- [Docker](https://docker.com) + Docker Compose
- At least 2GB RAM
- (Optional) OpenRouter API key for embeddings

### 1. Clone & Configure

```bash
git clone https://github.com/benclawbot/open-brain.git
cd open-brain

# Copy environment file
cp .env.example .env
```

### 2. Set Environment Variables

Edit `.env`:

```env
# Database
DB_PASSWORD=your_secure_password

# Embeddings (OpenRouter = FREE)
OPENROUTER_API_KEY=your_openrouter_key

# Optional: Telegram notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

> **No API key?** OpenRouter has a free tier. Just sign up at [openrouter.ai](https://openrouter.ai).

### 3. Start Everything

```bash
docker compose up -d
```

### 4. Access Services

| Service | URL | Description |
|---------|-----|-------------|
| **Web App** | http://localhost:3777 | Next.js UI |
| **MCP Server** | http://localhost:8080 | MCP protocol |
| **REST API** | http://localhost:8000 | HTTP API |
| **API Docs** | http://localhost:8000/docs | Swagger docs |

---

## 🔧 Configuration

All settings in `config/settings.yaml`:

```yaml
database:
  host: postgres
  port: 5432
  name: openbrain
  user: postgres
  password: ${DB_PASSWORD}

embedder:
  # Providers: openrouter, openai, ollama, custom
  provider: openrouter
  model: text-embedding-3-small
  dimensions: 768

mcp:
  host: 0.0.0.0
  port: 8080

api:
  host: 0.0.0.0
  port: 8000

web:
  port: 3777
```

### Embedder Providers

| Provider | Env Variable | Notes |
|----------|-------------|-------|
| **OpenRouter** (default) | `OPENROUTER_API_KEY` | Free tier available |
| OpenAI | `OPENAI_API_KEY` | Paid |
| Ollama | `OLLAMA_BASE_URL` | Local, free |
| Custom | `CUSTOM_API_URL` + `CUSTOM_API_KEY` | Any OpenAI-compatible |

---

## 📡 Usage

### CLI

```bash
# Install
pip install -e .

# Search memories
openbrain search "what did I learn about AI"

# Store a memory
openbrain store "Meeting with Oliver about trading bot" --source telegram --tags ai,trading

# Show stats
openbrain stats

# Generate weekly report
openbrain report

# Start API server
openbrain serve
```

### MCP Tools

Connect any MCP client to `http://localhost:8080`:

```python
# Example: Using memory_search
{
  "name": "memory_search",
  "arguments": {
    "query": "trading strategies",
    "limit": 5,
    "sources": ["telegram", "claude"]
  }
}
```

### REST API

```bash
# Search
curl -X POST http://localhost:8000/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query": "AI agents", "limit": 5}'

# Store
curl -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -d '{"content": "New idea", "source": "manual"}'

# Stats
curl http://localhost:8000/stats
```

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Clients                               │
│   Claude Code | Codex | OpenClaw | Custom Apps | CLI       │
└─────────────────────────┬───────────────────────────────────┘
                          │ MCP / HTTP / CLI
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                      Open Brain                              │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │   MCP API   │  │  REST API   │  │     CLI Tools    │   │
│  └─────────────┘  └──────────────┘  └──────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              Application Layer                          │ │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │ │
│  │  │Extractors│ │  Tagger  │ │Analytics │ │ Notifier │  │ │
│  │  │ (Entities│ │ (Auto-tag│ │ (Trends) │ │(Telegram)│  │ │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘  │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              Embedder (Multi-Provider)                  │ │
│  │   OpenRouter | OpenAI | Ollama | Custom                 │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│                   PostgreSQL + pgvector                      │
│   memory table with vector embeddings, GIN indexes          │
│   for tags/entities, IVFFlat for similarity search          │
└─────────────────────────────────────────────────────────────┘
```

### Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| Database | PostgreSQL + pgvector | Storage + vector search |
| MCP Server | FastMCP | Tool interface for AI agents |
| REST API | FastAPI | HTTP access |
| CLI | Click | Terminal commands |
| Web App | Next.js + Base UI | Visual UI |
| Embedder | requests | Multi-provider embeddings |
| Extractors | NLTK/spaCy | Entity extraction |

---

## 📁 Project Structure

```
open-brain/
├── config/
│   └── settings.yaml          # Configuration
├── src/
│   ├── main.py                # MCP server entry
│   ├── db/                    # Database layer
│   │   ├── schema.sql
│   │   ├── connection.py
│   │   └── queries.py
│   ├── embedder/              # Multi-provider embeddings
│   ├── extractors/            # NER + tagging
│   ├── analytics/             # Trends + reports
│   ├── connectors/            # Source importers
│   ├── cli/                   # CLI commands
│   ├── api/                   # REST API
│   ├── notifications/         # Telegram + email
│   └── ingestion/             # Bulk import
├── web/                       # Next.js web app
├── scripts/
│   ├── setup_db.py            # Database setup
│   ├── backup.sh              # Automated backups
│   └── healthcheck.sh         # Health monitoring
├── tests/
│   └── test_core.py
├── docker-compose.yml         # Full stack
├── Dockerfile                 # App container
├── pyproject.toml             # CLI package
├── requirements.txt           # Python deps
└── README.md
```

---

## 🔌 Source Connectors

### Telegram

```python
from src.connectors.telegram import TelegramImporter

importer = TelegramImporter(
    export_file="telegram_export.json"
)
importer.import_all(db_conn)
```

### WhatsApp

```python
from src.connectors.whatsapp import WhatsAppImporter

importer = WhatsAppImporter(
    export_file="whatsapp_chat.txt"
)
importer.import_all(db_conn)
```

### Claude Code

```python
from src.connectors.claude_code import ClaudeCodeImporter

importer = ClaudeCodeImporter(
    sessions_path="~/.claude/sessions"
)
importer.import_all(db_conn)
```

### Gmail

```python
from src.connectors.gmail import GmailImporter

importer = GmailImporter(
    takeout_path="./mail"
)
importer.import_all(db_conn)
```

---

## 📊 Analytics

### Trend Detection

Automatically detects:
- **Emerging topics** — Tags increasing >50% vs baseline
- **Declining topics** — Tags dropping >30%
- **New entities** — People/places appearing for first time
- **Co-occurrence** — Topics that appear together

### Weekly Reports

Generated every Sunday via cron:

```markdown
# Weekly Memory Report

### Activity
- New memories: 47
- By source: telegram: 23, claude: 15, manual: 9

### What's Hot
- ai: +200% (15 mentions)
- trading: +50% (8 mentions)

### Insights
- You're researching AI agents heavily this week
- Oliver appeared 5 times — significant collaboration
```

---

## 🔔 Notifications

### Telegram Alerts

- New emerging trends
- Weekly reports
- Memory stats summaries

### Email

- Daily digests
- Weekly reports
- Anomaly alerts

---

## 🐳 Docker Services

| Service | Image | Ports |
|---------|-------|-------|
| postgres | pgvector/pgvector:0.5.1 | 5432 |
| api | benclawbot/open-brain | 8000 |
| web | local Next.js image | 3777 |
| mcp | benclawbot/open-brain | 8080 |

---

## 🤖 Phone Installation (ARM)

For running on an old Android phone converted to Linux:

```bash
# Use ARM-compatible images
docker-compose -f docker-compose.arm.yml up -d

# Or build locally on phone
docker build --platform=linux/arm64 .
```

Resources: ~500MB RAM, ~1GB storage

---

## 🧪 Testing

```bash
# Run tests
pytest tests/

# Test MCP connection
python -c "from src.main import mcp; print(mcp)"
```

---

## 📝 License

MIT License — do whatever you want with it.

---

## 🙏 Acknowledgments

- [pgvector](https://github.com/pgvector/pgvector) — Vector similarity for PostgreSQL
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP framework
- [OpenRouter](https://openrouter.ai) — Free embedding API

---

## 🔗 Links

- [GitHub](https://github.com/benclawbot/open-brain)
- [Report Issues](https://github.com/benclawbot/open-brain/issues)
- [Discussions](https://github.com/benclawbot/open-brain/discussions)
