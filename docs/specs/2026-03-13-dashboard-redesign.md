# Open Brain Dashboard Redesign — Design Spec

**Date:** 2026-03-13
**Status:** Approved
**Goal:** Replace the minimal Streamlit dashboard with a modern React frontend for personal daily-driver knowledge management.

---

## Architecture

```
Next.js 15 Frontend (port 3777)
  ├── App Router (/search, /timeline, /entities, /graph, /settings)
  ├── shadcn/ui + Tailwind CSS 4 (dark mode default)
  ├── Cytoscape.js (knowledge graph)
  ├── React Query v5 (API state)
  └── cmdk (⌘K quick capture)
         │
         │ HTTP/JSON
         ▼
FastAPI Backend (port 8000) — existing + 6 new endpoints
         │
         ▼
PostgreSQL + pgvector (port 5433) — unchanged
```

Frontend location: `/home/mihai/tools/open-brain/web/`
Streamlit UI stays at `ui/` as fallback until new dashboard is stable.

## Tech Stack

| Layer | Choice |
|-------|--------|
| Framework | Next.js 15 (App Router) |
| Styling | Tailwind CSS 4 + shadcn/ui (dark mode default) |
| State | TanStack React Query v5 |
| Graph | Cytoscape.js + cose-bilkent layout |
| Quick capture | cmdk (⌘K command palette) |
| Icons | Lucide React |
| Dates | date-fns |
| HTTP | Native fetch |
| Port | 3777 |

No authentication (localhost-only personal tool).

## Pages

### Global Layout

Dark sidebar (240px) + main content area.

**Sidebar contents:**
- Logo + "Open Brain" title
- Quick stats: total memories, this week count
- Nav links: Search, Timeline, Entities, Graph, Settings
- Quick capture button (also ⌘K globally)

### 1. `/search` (default landing)

- Search input with debounced instant results (300ms)
- Filter bar: source dropdown, tag multi-select, date range picker, importance slider
- Results as cards: content preview (2 lines), source badge, tag pills, date, importance indicator
- Click card → expandable detail panel with full content, edit capability, delete
- Calls existing `POST /memories/search` (requires extending `SearchRequest` with `date_from`, `date_to`, `importance_min` fields — the underlying `search_memories()` in `queries.py` already supports date params, importance filter needs adding)

### 2. `/timeline`

- Vertical timeline grouped by day, newest first
- Each day shows memory cards in chronological order
- Scroll-based infinite loading (7 days per page)
- Date picker to jump to a specific date
- Calls new `GET /memories/timeline?from=&to=`

### 3. `/entities`

- Three-column drill-down: entity types → entity names → memories
- Entity types: all types present in the data (the extractor produces: people, organizations, locations, technologies, projects, emails, urls, phones, dates, hashtags, mentions — show whatever has data)
- Click type → distinct entity names with memory count
- Click entity name → memories referencing that entity
- Calls `GET /entities/types`, `GET /entities/names?type=`, `GET /entities/memories?type=&name=`

### 4. `/graph`

- Full-width Cytoscape.js canvas
- Nodes = entities, colored by type (people=blue, projects=green, tech=purple, orgs=orange, locations=red)
- Edges = entity co-occurrence within same memory (weight = count)
- Force-directed layout (cose-bilkent)
- Click node → sidebar with entity details + related memories
- Zoom, pan, search within graph
- Filter by entity type checkboxes
- Calls new `GET /memories/graph?types=&limit=`

### 5. `/settings`

- Form matching current Streamlit settings: database, embedder, ports, security
- Reads from `GET /settings`, saves via `PUT /settings` (backend reads/writes `config/settings.yaml`)

### Quick Capture (⌘K)

- cmdk command palette overlay, available on all pages
- Type content → Enter to save as memory (source: "dashboard", auto-tagged)
- Phase 1: quick capture only (text → save). Slash commands deferred to Phase 2.

## New Backend Endpoints

Added to existing FastAPI at `src/api/main.py`.

### `POST /memories/search` (existing — extend)

Add to `SearchRequest`: `date_from` (optional ISO date), `date_to` (optional ISO date), `importance_min` (optional float 0-1). Wire `date_from`/`date_to` through to existing `search_memories()` params. Add `importance_min` filter as `WHERE importance >= $N`.

### `GET /memories/timeline`

**Params:** `from` (date, default 7 days ago), `to` (date, default today), `limit_per_day` (int, default 50)
**Returns:**

```json
{
  "days": {
    "2026-03-13": [{ "id": "...", "content": "...", ... }],
    "2026-03-12": [{ "id": "...", "content": "...", ... }]
  },
  "has_more": true
}
```

Dates are server-local time. Newest day first. Memories within each day by `created_at` desc. `has_more` is true if there are days before the `from` date with data.

### `GET /entities/types`

**Returns:** `{ "people": 45, "technologies": 120, "projects": 30, ... }` — all entity types present in the data with memory count.

### `GET /entities/names?type=people&limit=50`

**Returns:** `[{ "name": "Mihai", "count": 85 }, ...]` — distinct entity names for the given type, sorted by count desc.

### `GET /entities/memories?type=people&name=Mihai&limit=20`

**Returns:** Array of memory objects that contain the given entity.

### `GET /memories/graph`

**Params:** `types` (comma-separated entity types, default all), `limit` (max nodes, default 200)

**Returns:** Cytoscape-compatible graph data.

```json
{
  "nodes": [
    { "id": "person:Mihai", "label": "Mihai", "type": "people", "count": 85 }
  ],
  "edges": [
    { "source": "person:Mihai", "target": "project:quarticle", "weight": 35 }
  ]
}
```

Nodes ranked by total memory count (most referenced first); `limit` selects top-N nodes. Edges computed only among selected nodes by counting entity co-occurrences within the same memory.

### `GET /settings`

**Returns:** Current `config/settings.yaml` as JSON object.

### `PUT /settings`

**Body:** JSON settings object. Writes to `config/settings.yaml`. Returns `{ "status": "saved" }`.

## Deployment

- Systemd user service `openbrain-web.service` on port 3777
- Runs `npm run start` (Next.js production) or `npm run dev` for development
- Existing `openbrain-api.service` and `openbrain-dashboard.service` unchanged
- Streamlit dashboard remains available as fallback on port 8501
