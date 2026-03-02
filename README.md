# Open Brain 🧠

A standalone, Docker-based personal memory management system with semantic search, analytics, and a beautiful dashboard.

## Features

- **Semantic Memory Storage**: Store memories with AI-powered embeddings
- **Multiple Data Sources**: Import from Telegram, WhatsApp, Claude Code, Gmail, and local Markdown files
- **REST API**: Full FastAPI server for programmatic access
- **Web Dashboard**: Streamlit-based UI for browsing and managing memories
- **Analytics**: Weekly reports, trend analysis, and statistics
- **Notifications**: Telegram and email alerts
- **Docker-Ready**: One command to start everything

## Quick Start

### Prerequisites

- Docker & Docker Compose
- PostgreSQL (handled by Docker)
- Ollama for embeddings (optional, for semantic search)

### 1. Clone and Setup

```bash
cd /home/tom/.openclaw/workspace
git clone <repo> openbrain
cd openbrain
```

### 2. Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your settings
nano .env
```

Required environment variables:
- `DB_PASSWORD` - PostgreSQL password
- `OLLAMA_BASE_URL` - Ollama server URL (default: http://localhost:11434)
- `EMBEDDER_MODEL` - Embedding model (default: nomic-embed-text)

Optional:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - For Telegram notifications
- `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` - For email notifications

### 3. Start Everything

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f

# Stop everything
docker-compose down
```

### 4. Access the Services

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:8501 |
| API Docs | http://localhost:8000/docs |
| API | http://localhost:8000 |

## CLI Usage

Install the CLI:

```bash
pip install -e .
```

### Commands

```bash
# Search memories
openbrain search "what did I learn about Python"

# Store a new memory
openbrain store "Remembered to water the plants" --tag reminder

# Show statistics
openbrain stats

# Import from source
openbrain import telegram /path/to/telegram/export
openbrain import whatsapp /path/to/chat.txt
openbrain import gmail /path/to/gmail/takeout
openbrain import file /path/to/notes/

# Generate weekly report
openbrain report --days 7

# Start API server
openbrain serve --port 8000
```

## REST API

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/memories` | List all memories |
| POST | `/memories` | Create new memory |
| GET | `/memories/{id}` | Get specific memory |
| POST | `/memories/search` | Search memories |
| GET | `/stats` | Get statistics |
| GET | `/trends` | Get trending topics |
| GET | `/report/weekly` | Get weekly report |

### Example

```bash
# Search memories
curl -X POST http://localhost:8000/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query": "learning", "limit": 5}'

# Store memory
curl -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -d '{"content": "My new memory", "source": "api"}'
```

## Data Import

### Telegram
Export your Telegram data and import:
```bash
openbrain import telegram /path/to/telegram_export
```

### WhatsApp
Export a chat and import:
```bash
openbrain import whatsapp /path/to/chat.txt
```

### Claude Code
Point to your Claude Code session logs:
```bash
openbrain import claude_code /path/to/claude_sessions
```

### Gmail
Import from Gmail takeout:
```bash
openbrain import gmail /path/to/gmail_takeout
```

### File Watcher
Watch a folder for new Markdown files:
```bash
# One-time import
openbrain import file /path/to/notes/

# Or watch continuously
python -m src.connectors.file_watcher /path/to/notes
```

## Docker Development

### Development Mode

```bash
# Start with hot reload
docker-compose up

# Rebuild specific service
docker-compose build api
docker-compose up api
```

### Production Mode

```bash
# Use production docker-compose file
docker-compose -f docker-compose.prod.yml up -d
```

## Configuration

All configuration is in `config/settings.yaml`:

```yaml
database:
  host: postgres
  port: 5432
  name: openbrain
  user: postgres
  password: ${DB_PASSWORD}

embedder:
  provider: ollama
  model: nomic-embed-text
  dimensions: 768
  base_url: http://host.docker.internal:11434

mcp:
  host: 0.0.0.0
  port: 8080

tags:
  deny_list:
    - password
    - secret
  default_tags:
    - auto

analytics:
  trend_weeks: 4
  weekly_report_day: 0
```

## Scripts

### Backup
```bash
# Run backup
./scripts/backup.sh

# Schedule daily backups (cron)
0 2 * * * /path/to/openbrain/scripts/backup.sh
```

### Health Check
```bash
# Run health check
./scripts/healthcheck.sh

# Schedule regular checks (cron)
*/15 * * * * /path/to/openbrain/scripts/healthcheck.sh
```

## Project Structure

```
openbrain/
├── config/              # Configuration files
│   └── settings.yaml
├── data/                # Data storage (backups, uploads)
├── scripts/             # Utility scripts
│   ├── backup.sh
│   └── healthcheck.sh
├── src/
│   ├── api/            # REST API (FastAPI)
│   │   └── main.py
│   ├── cli/            # CLI tools
│   │   ├── __init__.py
│   │   ├── search.py
│   │   ├── store.py
│   │   ├── stats.py
│   │   └── ...
│   ├── connectors/     # Data source connectors
│   │   ├── telegram.py
│   │   ├── whatsapp.py
│   │   ├── gmail.py
│   │   └── file_watcher.py
│   ├── notifications/   # Notification handlers
│   │   ├── telegram_bot.py
│   │   └── email_notifier.py
│   ├── db/             # Database layer
│   ├── embedder/       # Embedding generation
│   ├── extractors/     # Entity/tag extraction
│   └── analytics/     # Trends & reports
├── ui/                 # Streamlit dashboard
│   └── dashboard.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

## Troubleshooting

### Database Connection Failed
```bash
# Check if PostgreSQL is running
docker-compose ps

# Check logs
docker-compose logs postgres
```

### Embeddings Not Working
```bash
# Check Ollama
curl http://localhost:11434/api/tags

# Pull the model
ollama pull nomic-embed-text
```

### API Not Responding
```bash
# Check API logs
docker-compose logs api

# Check if port is exposed
docker-compose port api 8000
```

## License

MIT License - Feel free to use and modify!
