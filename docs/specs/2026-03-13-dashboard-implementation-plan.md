# Open Brain Dashboard Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Streamlit dashboard with a Next.js 15 React frontend featuring semantic search with filters, timeline view, entity explorer, knowledge graph, and quick capture.

**Architecture:** Next.js 15 App Router frontend (port 3777) calling the existing FastAPI backend (port 8000) with 6 new endpoints. PostgreSQL + pgvector unchanged. Frontend at `/home/mihai/tools/open-brain/web/`.

**Tech Stack:** Next.js 15, Tailwind CSS 4, shadcn/ui, TanStack React Query v5, Cytoscape.js, cmdk, Lucide React, date-fns

**Spec:** `/home/mihai/tools/open-brain/docs/specs/2026-03-13-dashboard-redesign.md`

---

## Chunk 1: Backend — New API Endpoints

### Task 1: Extend search endpoint with date and importance filters

**Files:**
- Modify: `/home/mihai/tools/open-brain/src/api/main.py:97-101` (SearchRequest model)
- Modify: `/home/mihai/tools/open-brain/src/api/main.py:242-266` (search endpoint)

- [ ] **Step 1: Add date and importance fields to SearchRequest**

In `src/api/main.py`, update the `SearchRequest` model:

```python
class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    sources: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    date_from: Optional[str] = None  # ISO date string YYYY-MM-DD
    date_to: Optional[str] = None
    importance_min: Optional[float] = None  # 0.0 to 1.0
```

- [ ] **Step 2: Wire new filters through to search_memories()**

Update the `search_memories_endpoint` function to pass the new fields:

```python
@app.post("/memories/search")
async def search_memories_endpoint(search: SearchRequest):
    query = search.query
    limit = search.limit
    sources = search.sources
    tags = search.tags

    # Parse dates
    date_from = None
    date_to = None
    if search.date_from:
        from datetime import datetime
        date_from = datetime.fromisoformat(search.date_from)
    if search.date_to:
        from datetime import datetime
        date_to = datetime.fromisoformat(search.date_to)

    embedding = None
    if query:
        try:
            embedding = create_embedding(query)
        except Exception as e:
            print(f"Warning: Could not create embedding: {e}")

    results = search_memories(
        query=query,
        embedding=embedding,
        limit=limit,
        sources=sources,
        tags=tags,
        date_from=date_from,
        date_to=date_to
    )

    # Apply importance filter (post-query since search_memories doesn't support it)
    if search.importance_min is not None:
        results = [m for m in results if m.get("importance", 0) >= search.importance_min]

    return [MemoryResponse.from_memory(m) for m in results]
```

- [ ] **Step 3: Test manually**

```bash
curl -s -X POST http://localhost:8000/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query": "quarticle", "limit": 5, "date_from": "2026-03-01", "importance_min": 0.5}' | python3 -m json.tool
```

Expected: Returns memories matching "quarticle" from March 2026 with importance >= 0.5.

- [ ] **Step 4: Commit**

```bash
cd /home/mihai/tools/open-brain
git add src/api/main.py
git commit -m "feat(api): extend search with date_from, date_to, importance_min filters"
```

---

### Task 2: Add timeline endpoint

**Files:**
- Modify: `/home/mihai/tools/open-brain/src/db/queries.py` (add query function)
- Modify: `/home/mihai/tools/open-brain/src/api/main.py` (add route)

- [ ] **Step 1: Add get_timeline_memories() to queries.py**

Append to `src/db/queries.py`:

```python
def get_timeline_memories(
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit_per_day: int = 50
) -> Dict[str, List[Dict[str, Any]]]:
    """Get memories grouped by date for timeline view."""
    from datetime import timedelta

    if date_to is None:
        date_to = datetime.now()
    if date_from is None:
        date_from = date_to - timedelta(days=7)

    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, content, tags, entities, importance,
                   created_at, original_date, metadata,
                   created_at::date AS day
            FROM memory
            WHERE created_at >= %s AND created_at <= %s
            ORDER BY created_at DESC
        """, (date_from, date_to))
        results = cursor.fetchall()

    # Check if there are older memories
    has_more = False
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM memory WHERE created_at < %s) AS has_more",
            (date_from,)
        )
        has_more = cursor.fetchone()['has_more']

    # Group by date string
    days: Dict[str, list] = {}
    day_counts: Dict[str, int] = {}
    for row in results:
        mem = dict(row)
        if isinstance(mem.get('entities'), str):
            mem['entities'] = json.loads(mem['entities'])
        if isinstance(mem.get('metadata'), str):
            mem['metadata'] = json.loads(mem['metadata'])
        day_key = str(mem.pop('day'))
        day_counts.setdefault(day_key, 0)
        day_counts[day_key] += 1
        if day_counts[day_key] <= limit_per_day:
            days.setdefault(day_key, []).append(mem)

    return {"days": days, "has_more": has_more}
```

- [ ] **Step 2: Add timeline route to api/main.py**

Add import at top of `src/api/main.py`:
```python
from db.queries import (
    search_memories, get_memory_by_id, insert_memory, delete_memory,
    update_memory_tags, update_memory, get_memory_stats,
    get_today_memories, get_recent_memories, get_timeline_memories
)
```

Add route before `if __name__`:
```python
@app.get("/memories/timeline")
async def get_timeline(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit_per_day: int = Query(50, le=200)
):
    """Get memories grouped by date for timeline view."""
    from datetime import datetime as dt
    df = dt.fromisoformat(date_from) if date_from else None
    dt_to = dt.fromisoformat(date_to) if date_to else None
    return get_timeline_memories(df, dt_to, limit_per_day)
```

- [ ] **Step 3: Test manually**

```bash
curl -s http://localhost:8000/memories/timeline | python3 -m json.tool
```

Expected: JSON with `days` dict (date keys → memory arrays) and `has_more` boolean.

- [ ] **Step 4: Commit**

```bash
git add src/db/queries.py src/api/main.py
git commit -m "feat(api): add GET /memories/timeline endpoint"
```

---

### Task 3: Add entity endpoints

**Files:**
- Modify: `/home/mihai/tools/open-brain/src/db/queries.py` (add query functions)
- Modify: `/home/mihai/tools/open-brain/src/api/main.py` (add routes)

- [ ] **Step 1: Add entity query functions to queries.py**

Append to `src/db/queries.py`:

```python
def get_entity_type_counts() -> Dict[str, int]:
    """Get count of memories per entity type."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT key AS entity_type, COUNT(DISTINCT m.id) AS count
            FROM memory m, jsonb_each(m.entities) AS e(key, value)
            WHERE jsonb_typeof(e.value) = 'array' AND jsonb_array_length(e.value) > 0
            GROUP BY key
            ORDER BY count DESC
        """)
        return {row['entity_type']: row['count'] for row in cursor.fetchall()}


def get_entity_names(entity_type: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Get distinct entity names for a given type with memory count."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT elem::text AS name, COUNT(DISTINCT m.id) AS count
            FROM memory m, jsonb_array_elements_text(m.entities->%s) AS elem
            GROUP BY elem
            ORDER BY count DESC
            LIMIT %s
        """, (entity_type, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_entity_memories(entity_type: str, entity_name: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Get memories containing a specific entity."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, content, tags, entities, importance,
                   created_at, original_date, metadata
            FROM memory
            WHERE entities->%s ? %s
            ORDER BY importance DESC, created_at DESC
            LIMIT %s
        """, (entity_type, entity_name, limit))
        results = cursor.fetchall()

    memories = []
    for row in results:
        mem = dict(row)
        if isinstance(mem.get('entities'), str):
            mem['entities'] = json.loads(mem['entities'])
        if isinstance(mem.get('metadata'), str):
            mem['metadata'] = json.loads(mem['metadata'])
        memories.append(mem)
    return memories
```

- [ ] **Step 2: Add entity routes to api/main.py**

Update import:
```python
from db.queries import (
    search_memories, get_memory_by_id, insert_memory, delete_memory,
    update_memory_tags, update_memory, get_memory_stats,
    get_today_memories, get_recent_memories, get_timeline_memories,
    get_entity_type_counts, get_entity_names, get_entity_memories
)
```

Add routes:
```python
@app.get("/entities/types")
async def entity_types():
    """Get entity type summary with counts."""
    return get_entity_type_counts()


@app.get("/entities/names")
async def entity_names(type: str = Query(...), limit: int = Query(50, le=200)):
    """Get distinct entity names for a type."""
    return get_entity_names(type, limit)


@app.get("/entities/memories")
async def entity_memories(
    type: str = Query(...),
    name: str = Query(...),
    limit: int = Query(20, le=100)
):
    """Get memories containing a specific entity."""
    results = get_entity_memories(type, name, limit)
    return [MemoryResponse.from_memory(m) for m in results]
```

- [ ] **Step 3: Test manually**

```bash
curl -s http://localhost:8000/entities/types | python3 -m json.tool
curl -s "http://localhost:8000/entities/names?type=technologies&limit=10" | python3 -m json.tool
curl -s "http://localhost:8000/entities/memories?type=technologies&name=kubernetes&limit=5" | python3 -m json.tool
```

- [ ] **Step 4: Commit**

```bash
git add src/db/queries.py src/api/main.py
git commit -m "feat(api): add entity endpoints (types, names, memories)"
```

---

### Task 4: Add graph endpoint

**Files:**
- Modify: `/home/mihai/tools/open-brain/src/db/queries.py`
- Modify: `/home/mihai/tools/open-brain/src/api/main.py`

- [ ] **Step 1: Add graph query function to queries.py**

Append to `src/db/queries.py`:

```python
def get_entity_graph(
    types: Optional[List[str]] = None,
    limit: int = 200
) -> Dict[str, Any]:
    """Build entity co-occurrence graph for Cytoscape.js visualization."""
    from collections import defaultdict

    # Get all memories with entities
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, entities FROM memory
            WHERE entities != '{}'::jsonb
        """)
        rows = cursor.fetchall()

    # Count entity occurrences and co-occurrences
    entity_counts: Dict[str, int] = defaultdict(int)
    edge_counts: Dict[tuple, int] = defaultdict(int)

    for row in rows:
        entities_data = row['entities']
        if isinstance(entities_data, str):
            entities_data = json.loads(entities_data)

        # Collect all entities from this memory
        memory_entities = []
        for etype, names in entities_data.items():
            if types and etype not in types:
                continue
            if isinstance(names, list):
                for name in names:
                    node_id = f"{etype}:{name}"
                    entity_counts[node_id] += 1
                    memory_entities.append((node_id, name, etype))

        # Build co-occurrence edges
        for i, (id_a, _, _) in enumerate(memory_entities):
            for j in range(i + 1, len(memory_entities)):
                id_b = memory_entities[j][0]
                if id_a != id_b:
                    edge_key = tuple(sorted([id_a, id_b]))
                    edge_counts[edge_key] += 1

    # Select top-N nodes by count
    sorted_nodes = sorted(entity_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    selected_ids = {node_id for node_id, _ in sorted_nodes}

    nodes = []
    for node_id, count in sorted_nodes:
        parts = node_id.split(":", 1)
        nodes.append({
            "id": node_id,
            "label": parts[1] if len(parts) > 1 else node_id,
            "type": parts[0] if len(parts) > 1 else "unknown",
            "count": count
        })

    edges = []
    for (source, target), weight in edge_counts.items():
        if source in selected_ids and target in selected_ids:
            edges.append({"source": source, "target": target, "weight": weight})

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 2: Add graph route to api/main.py**

Update import:
```python
from db.queries import (
    search_memories, get_memory_by_id, insert_memory, delete_memory,
    update_memory_tags, update_memory, get_memory_stats,
    get_today_memories, get_recent_memories, get_timeline_memories,
    get_entity_type_counts, get_entity_names, get_entity_memories,
    get_entity_graph
)
```

Add route:
```python
@app.get("/memories/graph")
async def memories_graph(
    types: Optional[str] = None,
    limit: int = Query(200, le=500)
):
    """Get entity co-occurrence graph for visualization."""
    type_list = types.split(",") if types else None
    return get_entity_graph(type_list, limit)
```

- [ ] **Step 3: Test manually**

```bash
curl -s "http://localhost:8000/memories/graph?limit=50" | python3 -m json.tool
```

Expected: JSON with `nodes` array and `edges` array.

- [ ] **Step 4: Commit**

```bash
git add src/db/queries.py src/api/main.py
git commit -m "feat(api): add GET /memories/graph entity co-occurrence endpoint"
```

---

### Task 5: Add settings endpoints

**Files:**
- Modify: `/home/mihai/tools/open-brain/src/api/main.py`

- [ ] **Step 1: Add settings routes**

```python
import yaml
from pathlib import Path

SETTINGS_PATH = Path(__file__).parent.parent.parent / "config" / "settings.yaml"


@app.get("/settings")
async def get_settings():
    """Get current settings."""
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


@app.put("/settings")
async def update_settings(settings: dict):
    """Update settings."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        yaml.dump(settings, f, default_flow_style=False)
    return {"status": "saved"}
```

- [ ] **Step 2: Test manually**

```bash
curl -s http://localhost:8000/settings | python3 -m json.tool
```

- [ ] **Step 3: Commit**

```bash
git add src/api/main.py
git commit -m "feat(api): add GET/PUT /settings endpoints"
```

- [ ] **Step 4: Restart API service**

```bash
systemctl --user restart openbrain-api.service
```

---

## Chunk 2: Frontend — Project Scaffold & Layout

### Task 6: Initialize Next.js project

**Files:**
- Create: `/home/mihai/tools/open-brain/web/` (entire directory)

- [ ] **Step 1: Create Next.js app**

```bash
cd /home/mihai/tools/open-brain
npx create-next-app@latest web --typescript --tailwind --eslint --app --src-dir --no-import-alias --turbopack
```

When prompted: use defaults (Yes to all).

- [ ] **Step 2: Install dependencies**

```bash
cd /home/mihai/tools/open-brain/web
npm install @tanstack/react-query cytoscape cytoscape-cose-bilkent cmdk date-fns lucide-react
npm install -D @types/cytoscape
```

- [ ] **Step 3: Install shadcn/ui**

```bash
cd /home/mihai/tools/open-brain/web
npx shadcn@latest init -d
```

Select: New York style, Zinc color, CSS variables: yes.

- [ ] **Step 4: Add shadcn components we need**

```bash
npx shadcn@latest add button input select badge dialog slider card command separator scroll-area
```

- [ ] **Step 5: Set port to 3777**

Edit `web/package.json`, change the `dev` script:

```json
"dev": "next dev --turbopack -p 3777",
"start": "next start -p 3777"
```

- [ ] **Step 6: Verify it runs**

```bash
cd /home/mihai/tools/open-brain/web && npm run dev &
sleep 5
curl -s http://localhost:3777 | head -5
kill %1
```

- [ ] **Step 7: Commit**

```bash
cd /home/mihai/tools/open-brain
git add web/
git commit -m "feat(web): initialize Next.js 15 project with shadcn/ui"
```

---

### Task 7: Configure dark theme and API client

**Files:**
- Modify: `/home/mihai/tools/open-brain/web/src/app/globals.css`
- Create: `/home/mihai/tools/open-brain/web/src/lib/api.ts`
- Create: `/home/mihai/tools/open-brain/web/src/lib/providers.tsx`
- Modify: `/home/mihai/tools/open-brain/web/src/app/layout.tsx`

- [ ] **Step 1: Set dark theme as default in globals.css**

Replace the `:root` section in `globals.css` to force dark mode. Keep the shadcn CSS variables but ensure `dark` class is default on `<html>`.

- [ ] **Step 2: Create API client**

Create `web/src/lib/api.ts`:

```typescript
const API_BASE = "http://localhost:8000";

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// Types
export interface Memory {
  id: string;
  source: string;
  content: string;
  tags: string[];
  entities: Record<string, string[]>;
  importance: number;
  created_at: string;
}

export interface SearchParams {
  query: string;
  limit?: number;
  sources?: string[];
  tags?: string[];
  date_from?: string;
  date_to?: string;
  importance_min?: number;
}

export interface TimelineResponse {
  days: Record<string, Memory[]>;
  has_more: boolean;
}

export interface GraphData {
  nodes: { id: string; label: string; type: string; count: number }[];
  edges: { source: string; target: string; weight: number }[];
}

export interface Stats {
  total: number;
  this_week: number;
  this_month: number;
  by_source: Record<string, number>;
  top_tags: Record<string, number>;
}

// API functions
export const api = {
  search: (params: SearchParams) =>
    apiFetch<Memory[]>("/memories/search", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  getMemory: (id: string) => apiFetch<Memory>(`/memories/${id}`),

  createMemory: (content: string, source = "dashboard", tags: string[] = []) =>
    apiFetch<{ id: string; status: string; tags: string[] }>("/memories", {
      method: "POST",
      body: JSON.stringify({ content, source, tags }),
    }),

  updateMemory: (id: string, data: { content: string; tags: string[]; source: string; importance: number }) =>
    apiFetch<{ status: string }>(`/memories/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  deleteMemory: (id: string) =>
    apiFetch<{ status: string }>(`/memories/${id}`, { method: "DELETE" }),

  stats: () => apiFetch<Stats>("/stats"),

  timeline: (from?: string, to?: string, limitPerDay = 50) => {
    const params = new URLSearchParams();
    if (from) params.set("date_from", from);
    if (to) params.set("date_to", to);
    params.set("limit_per_day", String(limitPerDay));
    return apiFetch<TimelineResponse>(`/memories/timeline?${params}`);
  },

  entityTypes: () => apiFetch<Record<string, number>>("/entities/types"),

  entityNames: (type: string, limit = 50) =>
    apiFetch<{ name: string; count: number }[]>(`/entities/names?type=${type}&limit=${limit}`),

  entityMemories: (type: string, name: string, limit = 20) =>
    apiFetch<Memory[]>(`/entities/memories?type=${type}&name=${name}&limit=${limit}`),

  graph: (types?: string[], limit = 200) => {
    const params = new URLSearchParams();
    if (types?.length) params.set("types", types.join(","));
    params.set("limit", String(limit));
    return apiFetch<GraphData>(`/memories/graph?${params}`);
  },

  getSettings: () => apiFetch<Record<string, unknown>>("/settings"),

  saveSettings: (settings: Record<string, unknown>) =>
    apiFetch<{ status: string }>("/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
};
```

- [ ] **Step 3: Create React Query provider**

Create `web/src/lib/providers.tsx`:

```tsx
"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () => new QueryClient({ defaultOptions: { queries: { staleTime: 30_000 } } })
  );
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
```

- [ ] **Step 4: Update layout.tsx to use providers and dark mode**

Replace `web/src/app/layout.tsx`:

```tsx
import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "@/lib/providers";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Open Brain",
  description: "Personal Semantic Memory System",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className={inter.className}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
```

- [ ] **Step 5: Commit**

```bash
cd /home/mihai/tools/open-brain
git add web/src/lib/ web/src/app/layout.tsx web/src/app/globals.css
git commit -m "feat(web): add API client, React Query provider, dark theme"
```

---

### Task 8: Build sidebar layout and navigation

**Files:**
- Create: `/home/mihai/tools/open-brain/web/src/components/sidebar.tsx`
- Create: `/home/mihai/tools/open-brain/web/src/components/quick-capture.tsx`
- Modify: `/home/mihai/tools/open-brain/web/src/app/layout.tsx`
- Create: `/home/mihai/tools/open-brain/web/src/app/(dashboard)/layout.tsx`

- [ ] **Step 1: Create sidebar component**

Create `web/src/components/sidebar.tsx`:

```tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Search, Clock, Users, Share2, Settings, Brain, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const navItems = [
  { href: "/search", label: "Search", icon: Search },
  { href: "/timeline", label: "Timeline", icon: Clock },
  { href: "/entities", label: "Entities", icon: Users },
  { href: "/graph", label: "Graph", icon: Share2 },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar({ onQuickCapture }: { onQuickCapture: () => void }) {
  const pathname = usePathname();
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 60_000 });

  return (
    <aside className="w-60 h-screen bg-zinc-950 border-r border-zinc-800 flex flex-col fixed left-0 top-0">
      <div className="p-4 flex items-center gap-3">
        <Brain className="h-6 w-6 text-blue-500" />
        <span className="font-semibold text-sm">Open Brain</span>
      </div>

      <div className="px-4 pb-4 flex gap-4 text-xs text-zinc-500">
        <div><span className="text-zinc-200 font-medium">{stats?.total ?? "—"}</span> memories</div>
        <div><span className="text-zinc-200 font-medium">{stats?.this_week ?? "—"}</span> this week</div>
      </div>

      <div className="border-t border-zinc-800" />

      <nav className="flex-1 p-2 space-y-1">
        {navItems.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className={cn(
              "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
              pathname === href
                ? "bg-zinc-800 text-zinc-100"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
            )}
          >
            <Icon className="h-4 w-4" />
            {label}
          </Link>
        ))}
      </nav>

      <div className="p-2 border-t border-zinc-800">
        <button
          onClick={onQuickCapture}
          className="flex items-center gap-3 px-3 py-2 w-full rounded-md text-sm text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900 transition-colors"
        >
          <Plus className="h-4 w-4" />
          Quick Capture
          <kbd className="ml-auto text-[10px] bg-zinc-800 px-1.5 py-0.5 rounded text-zinc-500">⌘K</kbd>
        </button>
      </div>
    </aside>
  );
}
```

- [ ] **Step 2: Create quick capture component**

Create `web/src/components/quick-capture.tsx`:

```tsx
"use client";

import { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CommandDialog, CommandInput } from "@/components/ui/command";
import { api } from "@/lib/api";

export function QuickCapture({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [value, setValue] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (content: string) => api.createMemory(content, "dashboard"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["stats"] });
      setValue("");
      onOpenChange(false);
    },
  });

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && value.trim() && !e.shiftKey) {
      e.preventDefault();
      mutation.mutate(value.trim());
    }
  };

  // Global ⌘K shortcut
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, [open, onOpenChange]);

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput
        placeholder="Capture a memory... (Enter to save)"
        value={value}
        onValueChange={setValue}
        onKeyDown={handleKeyDown}
      />
      {mutation.isPending && <div className="p-4 text-sm text-zinc-500">Saving...</div>}
      {mutation.isSuccess && <div className="p-4 text-sm text-green-500">Saved!</div>}
      {mutation.isError && <div className="p-4 text-sm text-red-500">Error saving memory</div>}
    </CommandDialog>
  );
}
```

- [ ] **Step 3: Create dashboard layout**

Create `web/src/app/(dashboard)/layout.tsx`:

```tsx
"use client";

import { useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { QuickCapture } from "@/components/quick-capture";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const [captureOpen, setCaptureOpen] = useState(false);

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <Sidebar onQuickCapture={() => setCaptureOpen(true)} />
      <QuickCapture open={captureOpen} onOpenChange={setCaptureOpen} />
      <main className="ml-60 p-6">{children}</main>
    </div>
  );
}
```

- [ ] **Step 4: Create redirect from / to /search**

Create `web/src/app/(dashboard)/page.tsx`:

```tsx
import { redirect } from "next/navigation";
export default function Home() { redirect("/search"); }
```

- [ ] **Step 5: Create placeholder pages**

Create 5 placeholder page files:

`web/src/app/(dashboard)/search/page.tsx`:
```tsx
export default function SearchPage() { return <h1 className="text-xl font-semibold">Search</h1>; }
```

`web/src/app/(dashboard)/timeline/page.tsx`:
```tsx
export default function TimelinePage() { return <h1 className="text-xl font-semibold">Timeline</h1>; }
```

`web/src/app/(dashboard)/entities/page.tsx`:
```tsx
export default function EntitiesPage() { return <h1 className="text-xl font-semibold">Entities</h1>; }
```

`web/src/app/(dashboard)/graph/page.tsx`:
```tsx
export default function GraphPage() { return <h1 className="text-xl font-semibold">Graph</h1>; }
```

`web/src/app/(dashboard)/settings/page.tsx`:
```tsx
export default function SettingsPage() { return <h1 className="text-xl font-semibold">Settings</h1>; }
```

- [ ] **Step 6: Verify layout works**

```bash
cd /home/mihai/tools/open-brain/web && npm run dev &
sleep 5
curl -s http://localhost:3777/search | grep "Open Brain"
kill %1
```

- [ ] **Step 7: Commit**

```bash
cd /home/mihai/tools/open-brain
git add web/src/components/ web/src/app/
git commit -m "feat(web): add sidebar layout, navigation, quick capture, placeholder pages"
```

---

## Chunk 3: Frontend — Search Page

### Task 9: Build the search page

**Files:**
- Create: `/home/mihai/tools/open-brain/web/src/app/(dashboard)/search/page.tsx`
- Create: `/home/mihai/tools/open-brain/web/src/components/memory-card.tsx`
- Create: `/home/mihai/tools/open-brain/web/src/components/memory-detail.tsx`

- [ ] **Step 1: Create reusable memory card component**

Create `web/src/components/memory-card.tsx`:

```tsx
"use client";

import { Memory } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { format } from "date-fns";

const sourceColors: Record<string, string> = {
  "claude-code": "bg-purple-900 text-purple-300",
  manual: "bg-blue-900 text-blue-300",
  telegram: "bg-sky-900 text-sky-300",
  dashboard: "bg-green-900 text-green-300",
  email: "bg-amber-900 text-amber-300",
};

export function MemoryCard({ memory, onClick }: { memory: Memory; onClick?: () => void }) {
  const date = memory.created_at ? format(new Date(memory.created_at), "MMM d, HH:mm") : "";

  return (
    <button
      onClick={onClick}
      className="w-full text-left p-4 rounded-lg border border-zinc-800 hover:border-zinc-600 bg-zinc-900/50 hover:bg-zinc-900 transition-colors"
    >
      <p className="text-sm text-zinc-300 line-clamp-2">{memory.content}</p>
      <div className="mt-2 flex items-center gap-2 flex-wrap">
        <Badge variant="outline" className={sourceColors[memory.source] ?? "bg-zinc-800 text-zinc-400"}>
          {memory.source}
        </Badge>
        {memory.tags.slice(0, 4).map((tag) => (
          <Badge key={tag} variant="secondary" className="text-[11px] bg-zinc-800 text-zinc-400">
            {tag}
          </Badge>
        ))}
        <span className="ml-auto text-[11px] text-zinc-600">{date}</span>
      </div>
    </button>
  );
}
```

- [ ] **Step 2: Create memory detail/edit panel**

Create `web/src/components/memory-detail.tsx`:

```tsx
"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Memory, api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Badge } from "@/components/ui/badge";
import { X, Save, Trash2 } from "lucide-react";
import { format } from "date-fns";

export function MemoryDetail({ memory, onClose }: { memory: Memory; onClose: () => void }) {
  const [content, setContent] = useState(memory.content);
  const [tags, setTags] = useState(memory.tags.join(", "));
  const [source, setSource] = useState(memory.source);
  const [importance, setImportance] = useState(memory.importance);
  const queryClient = useQueryClient();

  const updateMut = useMutation({
    mutationFn: () => api.updateMemory(memory.id, {
      content, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), source, importance,
    }),
    onSuccess: () => { queryClient.invalidateQueries(); onClose(); },
  });

  const deleteMut = useMutation({
    mutationFn: () => api.deleteMemory(memory.id),
    onSuccess: () => { queryClient.invalidateQueries(); onClose(); },
  });

  return (
    <div className="border-l border-zinc-800 bg-zinc-950 p-6 w-[480px] h-full overflow-y-auto fixed right-0 top-0">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-medium text-zinc-400">Memory Detail</h2>
        <Button variant="ghost" size="icon" onClick={onClose}><X className="h-4 w-4" /></Button>
      </div>

      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        className="w-full bg-zinc-900 border border-zinc-800 rounded-md p-3 text-sm text-zinc-200 min-h-[200px] resize-y"
      />

      <div className="mt-4 space-y-3">
        <div>
          <label className="text-xs text-zinc-500">Source</label>
          <Input value={source} onChange={(e) => setSource(e.target.value)} className="mt-1" />
        </div>
        <div>
          <label className="text-xs text-zinc-500">Tags (comma-separated)</label>
          <Input value={tags} onChange={(e) => setTags(e.target.value)} className="mt-1" />
        </div>
        <div>
          <label className="text-xs text-zinc-500">Importance: {importance.toFixed(2)}</label>
          <Slider value={[importance]} onValueChange={([v]) => setImportance(v)} min={0} max={1} step={0.05} className="mt-2" />
        </div>

        {memory.entities && Object.keys(memory.entities).length > 0 && (
          <div>
            <label className="text-xs text-zinc-500 mb-1 block">Entities</label>
            <div className="flex flex-wrap gap-1">
              {Object.entries(memory.entities).flatMap(([type, names]) =>
                (names as string[]).map((name) => (
                  <Badge key={`${type}:${name}`} variant="outline" className="text-[10px]">{type}: {name}</Badge>
                ))
              )}
            </div>
          </div>
        )}

        <div className="text-[11px] text-zinc-600">
          ID: {memory.id} &middot; Created: {memory.created_at ? format(new Date(memory.created_at), "PPpp") : "—"}
        </div>
      </div>

      <div className="mt-6 flex gap-2">
        <Button onClick={() => updateMut.mutate()} disabled={updateMut.isPending} className="flex-1">
          <Save className="h-4 w-4 mr-2" />{updateMut.isPending ? "Saving..." : "Save"}
        </Button>
        <Button variant="destructive" onClick={() => deleteMut.mutate()} disabled={deleteMut.isPending}>
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Build the search page**

Replace `web/src/app/(dashboard)/search/page.tsx`:

```tsx
"use client";

import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search as SearchIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { api, Memory } from "@/lib/api";
import { MemoryCard } from "@/components/memory-card";
import { MemoryDetail } from "@/components/memory-detail";
import { useDebounce } from "@/lib/hooks";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<string>("all");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [importanceMin, setImportanceMin] = useState(0);
  const [selected, setSelected] = useState<Memory | null>(null);

  const debouncedQuery = useDebounce(query, 300);

  const { data: results = [], isLoading } = useQuery({
    queryKey: ["search", debouncedQuery, source, dateFrom, dateTo, importanceMin],
    queryFn: () => api.search({
      query: debouncedQuery,
      limit: 50,
      sources: source !== "all" ? [source] : undefined,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      importance_min: importanceMin > 0 ? importanceMin : undefined,
    }),
    enabled: debouncedQuery.length > 0,
  });

  return (
    <div className="flex h-[calc(100vh-48px)]">
      <div className={`flex-1 ${selected ? "mr-[480px]" : ""}`}>
        <h1 className="text-xl font-semibold mb-4">Search</h1>

        <div className="relative mb-4">
          <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search memories..."
            className="pl-10"
          />
        </div>

        <div className="flex gap-3 mb-4 items-end flex-wrap">
          <div>
            <label className="text-[11px] text-zinc-500 mb-1 block">Source</label>
            <Select value={source} onValueChange={setSource}>
              <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                <SelectItem value="claude-code">Claude Code</SelectItem>
                <SelectItem value="manual">Manual</SelectItem>
                <SelectItem value="dashboard">Dashboard</SelectItem>
                <SelectItem value="telegram">Telegram</SelectItem>
                <SelectItem value="email">Email</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <label className="text-[11px] text-zinc-500 mb-1 block">From</label>
            <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-36" />
          </div>
          <div>
            <label className="text-[11px] text-zinc-500 mb-1 block">To</label>
            <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-36" />
          </div>
          <div className="w-40">
            <label className="text-[11px] text-zinc-500 mb-1 block">Min importance: {importanceMin.toFixed(1)}</label>
            <Slider value={[importanceMin]} onValueChange={([v]) => setImportanceMin(v)} min={0} max={1} step={0.1} />
          </div>
        </div>

        {isLoading && <p className="text-sm text-zinc-500">Searching...</p>}

        <div className="space-y-2">
          {results.map((mem) => (
            <MemoryCard key={mem.id} memory={mem} onClick={() => setSelected(mem)} />
          ))}
          {debouncedQuery && !isLoading && results.length === 0 && (
            <p className="text-sm text-zinc-500">No results found.</p>
          )}
        </div>
      </div>

      {selected && <MemoryDetail memory={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
```

- [ ] **Step 4: Create useDebounce hook**

Create `web/src/lib/hooks.ts`:

```typescript
import { useState, useEffect } from "react";

export function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}
```

- [ ] **Step 5: Commit**

```bash
cd /home/mihai/tools/open-brain
git add web/src/
git commit -m "feat(web): implement search page with filters, memory cards, detail panel"
```

---

## Chunk 4: Frontend — Timeline, Entities, Graph, Settings Pages

### Task 10: Build timeline page

**Files:**
- Replace: `/home/mihai/tools/open-brain/web/src/app/(dashboard)/timeline/page.tsx`

- [ ] **Step 1: Implement timeline page**

```tsx
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { format, subDays } from "date-fns";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { MemoryCard } from "@/components/memory-card";
import { MemoryDetail } from "@/components/memory-detail";
import type { Memory } from "@/lib/api";

export default function TimelinePage() {
  const [daysBack, setDaysBack] = useState(7);
  const [jumpDate, setJumpDate] = useState("");
  const [selected, setSelected] = useState<Memory | null>(null);

  const fromDate = jumpDate || format(subDays(new Date(), daysBack), "yyyy-MM-dd");
  const toDate = jumpDate ? format(new Date(jumpDate + "T23:59:59"), "yyyy-MM-dd") : format(new Date(), "yyyy-MM-dd");

  const { data, isLoading } = useQuery({
    queryKey: ["timeline", fromDate, toDate],
    queryFn: () => api.timeline(fromDate, toDate),
  });

  const days = data?.days ?? {};
  const sortedDays = Object.keys(days).sort().reverse();

  return (
    <div className="flex h-[calc(100vh-48px)]">
      <div className={`flex-1 ${selected ? "mr-[480px]" : ""}`}>
        <div className="flex items-center justify-between mb-4">
          <h1 className="text-xl font-semibold">Timeline</h1>
          <div className="flex items-center gap-3">
            <label className="text-[11px] text-zinc-500">Jump to date</label>
            <Input type="date" value={jumpDate} onChange={(e) => setJumpDate(e.target.value)} className="w-40" />
          </div>
        </div>

        {isLoading && <p className="text-sm text-zinc-500">Loading...</p>}

        <div className="space-y-6">
          {sortedDays.map((day) => (
            <div key={day}>
              <h2 className="text-sm font-medium text-zinc-400 mb-2 sticky top-0 bg-zinc-950 py-1">
                {format(new Date(day), "EEEE, MMMM d, yyyy")}
                <span className="ml-2 text-zinc-600">({days[day].length} memories)</span>
              </h2>
              <div className="space-y-2 pl-4 border-l border-zinc-800">
                {days[day].map((mem) => (
                  <MemoryCard key={mem.id} memory={mem} onClick={() => setSelected(mem)} />
                ))}
              </div>
            </div>
          ))}
        </div>

        {data?.has_more && !jumpDate && (
          <button
            onClick={() => setDaysBack((d) => d + 7)}
            className="mt-4 w-full py-2 text-sm text-zinc-500 hover:text-zinc-300 border border-zinc-800 rounded-md"
          >
            Load more...
          </button>
        )}
      </div>
      {selected && <MemoryDetail memory={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
cd /home/mihai/tools/open-brain && git add web/src/ && git commit -m "feat(web): implement timeline page"
```

---

### Task 11: Build entities page

**Files:**
- Replace: `/home/mihai/tools/open-brain/web/src/app/(dashboard)/entities/page.tsx`

- [ ] **Step 1: Implement entities page**

```tsx
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Memory } from "@/lib/api";
import { MemoryCard } from "@/components/memory-card";
import { MemoryDetail } from "@/components/memory-detail";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const typeIcons: Record<string, string> = {
  people: "👤", organizations: "🏢", locations: "📍", technologies: "💻", projects: "📁",
  emails: "📧", urls: "🔗", phones: "📱", dates: "📅", hashtags: "#", mentions: "@",
};

export default function EntitiesPage() {
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);

  const { data: types = {} } = useQuery({ queryKey: ["entityTypes"], queryFn: api.entityTypes });

  const { data: names = [] } = useQuery({
    queryKey: ["entityNames", selectedType],
    queryFn: () => api.entityNames(selectedType!, 100),
    enabled: !!selectedType,
  });

  const { data: memories = [] } = useQuery({
    queryKey: ["entityMemories", selectedType, selectedName],
    queryFn: () => api.entityMemories(selectedType!, selectedName!, 50),
    enabled: !!selectedType && !!selectedName,
  });

  return (
    <div className="flex h-[calc(100vh-48px)]">
      <div className={`flex-1 flex gap-0 ${selectedMemory ? "mr-[480px]" : ""}`}>
        {/* Column 1: Entity Types */}
        <div className="w-48 border-r border-zinc-800 pr-3 overflow-y-auto">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">Types</h2>
          {Object.entries(types).map(([type, count]) => (
            <button
              key={type}
              onClick={() => { setSelectedType(type); setSelectedName(null); }}
              className={cn(
                "w-full flex items-center justify-between px-2 py-1.5 rounded text-sm",
                selectedType === type ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900"
              )}
            >
              <span>{typeIcons[type] ?? "•"} {type}</span>
              <Badge variant="secondary" className="text-[10px]">{count}</Badge>
            </button>
          ))}
        </div>

        {/* Column 2: Entity Names */}
        <div className="w-64 border-r border-zinc-800 px-3 overflow-y-auto">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">
            {selectedType ?? "Select a type"}
          </h2>
          {names.map(({ name, count }) => (
            <button
              key={name}
              onClick={() => setSelectedName(name)}
              className={cn(
                "w-full flex items-center justify-between px-2 py-1.5 rounded text-sm",
                selectedName === name ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900"
              )}
            >
              <span className="truncate">{name}</span>
              <Badge variant="secondary" className="text-[10px] ml-2 shrink-0">{count}</Badge>
            </button>
          ))}
        </div>

        {/* Column 3: Memories */}
        <div className="flex-1 pl-3 overflow-y-auto">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">
            {selectedName ? `Memories with "${selectedName}"` : "Select an entity"}
          </h2>
          <div className="space-y-2">
            {memories.map((mem) => (
              <MemoryCard key={mem.id} memory={mem} onClick={() => setSelectedMemory(mem)} />
            ))}
          </div>
        </div>
      </div>
      {selectedMemory && <MemoryDetail memory={selectedMemory} onClose={() => setSelectedMemory(null)} />}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
cd /home/mihai/tools/open-brain && git add web/src/ && git commit -m "feat(web): implement entities drill-down page"
```

---

### Task 12: Build knowledge graph page

**Files:**
- Replace: `/home/mihai/tools/open-brain/web/src/app/(dashboard)/graph/page.tsx`
- Create: `/home/mihai/tools/open-brain/web/src/components/knowledge-graph.tsx`

- [ ] **Step 1: Create Cytoscape graph wrapper component**

Create `web/src/components/knowledge-graph.tsx`:

```tsx
"use client";

import { useEffect, useRef, useCallback } from "react";
import cytoscape, { Core } from "cytoscape";
import coseBilkent from "cytoscape-cose-bilkent";
import type { GraphData } from "@/lib/api";

cytoscape.use(coseBilkent);

const typeColors: Record<string, string> = {
  people: "#3b82f6", organizations: "#f97316", locations: "#ef4444",
  technologies: "#a855f7", projects: "#22c55e", emails: "#06b6d4",
  urls: "#64748b", phones: "#eab308", dates: "#ec4899",
  hashtags: "#8b5cf6", mentions: "#14b8a6",
};

export function KnowledgeGraph({
  data,
  onNodeClick,
}: {
  data: GraphData;
  onNodeClick?: (nodeId: string, label: string, type: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);

  const init = useCallback(() => {
    if (!containerRef.current || !data.nodes.length) return;

    if (cyRef.current) cyRef.current.destroy();

    const maxCount = Math.max(...data.nodes.map((n) => n.count), 1);

    cyRef.current = cytoscape({
      container: containerRef.current,
      elements: [
        ...data.nodes.map((n) => ({
          data: {
            id: n.id,
            label: n.label,
            type: n.type,
            size: 20 + (n.count / maxCount) * 40,
            color: typeColors[n.type] ?? "#6b7280",
          },
        })),
        ...data.edges.map((e) => ({
          data: { source: e.source, target: e.target, weight: e.weight },
        })),
      ],
      style: [
        {
          selector: "node",
          style: {
            label: "data(label)",
            "background-color": "data(color)",
            width: "data(size)",
            height: "data(size)",
            "font-size": "10px",
            color: "#d4d4d8",
            "text-valign": "bottom",
            "text-margin-y": 5,
            "overlay-opacity": 0,
          },
        },
        {
          selector: "edge",
          style: {
            width: "mapData(weight, 1, 20, 1, 6)",
            "line-color": "#333",
            opacity: 0.4,
            "curve-style": "bezier",
          },
        },
        {
          selector: "node:selected",
          style: { "border-width": 3, "border-color": "#fff" },
        },
      ],
      layout: { name: "cose-bilkent", animate: false, nodeRepulsion: 8000, idealEdgeLength: 120 } as any,
    });

    cyRef.current.on("tap", "node", (evt) => {
      const node = evt.target;
      onNodeClick?.(node.id(), node.data("label"), node.data("type"));
    });
  }, [data, onNodeClick]);

  useEffect(() => { init(); return () => { cyRef.current?.destroy(); }; }, [init]);

  return <div ref={containerRef} className="w-full h-full bg-zinc-950 rounded-lg border border-zinc-800" />;
}
```

- [ ] **Step 2: Build graph page**

Replace `web/src/app/(dashboard)/graph/page.tsx`:

```tsx
"use client";

import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Memory } from "@/lib/api";
import { KnowledgeGraph } from "@/components/knowledge-graph";
import { MemoryCard } from "@/components/memory-card";
import { Badge } from "@/components/ui/badge";

const allTypes = ["people", "organizations", "locations", "technologies", "projects"];

export default function GraphPage() {
  const [enabledTypes, setEnabledTypes] = useState<string[]>(allTypes);
  const [nodeLimit, setNodeLimit] = useState(150);
  const [selectedEntity, setSelectedEntity] = useState<{ type: string; name: string } | null>(null);

  const { data: graphData } = useQuery({
    queryKey: ["graph", enabledTypes, nodeLimit],
    queryFn: () => api.graph(enabledTypes, nodeLimit),
  });

  const { data: entityMemories = [] } = useQuery({
    queryKey: ["entityMemories", selectedEntity?.type, selectedEntity?.name],
    queryFn: () => api.entityMemories(selectedEntity!.type, selectedEntity!.name, 20),
    enabled: !!selectedEntity,
  });

  const handleNodeClick = useCallback((id: string, label: string, type: string) => {
    setSelectedEntity({ type, name: label });
  }, []);

  const toggleType = (type: string) => {
    setEnabledTypes((prev) =>
      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]
    );
  };

  return (
    <div className="flex h-[calc(100vh-48px)] gap-4">
      <div className="flex-1 flex flex-col">
        <div className="flex items-center justify-between mb-3">
          <h1 className="text-xl font-semibold">Knowledge Graph</h1>
          <div className="flex gap-2">
            {allTypes.map((type) => (
              <button
                key={type}
                onClick={() => toggleType(type)}
                className={`px-2 py-1 text-xs rounded border ${
                  enabledTypes.includes(type) ? "border-zinc-600 text-zinc-200" : "border-zinc-800 text-zinc-600"
                }`}
              >
                {type}
              </button>
            ))}
          </div>
        </div>
        <div className="flex-1">
          {graphData && <KnowledgeGraph data={graphData} onNodeClick={handleNodeClick} />}
        </div>
      </div>

      {selectedEntity && (
        <div className="w-80 border-l border-zinc-800 pl-4 overflow-y-auto">
          <h2 className="text-sm font-medium mb-1">{selectedEntity.name}</h2>
          <Badge variant="outline" className="text-[10px] mb-3">{selectedEntity.type}</Badge>
          <div className="space-y-2">
            {entityMemories.map((mem) => (
              <MemoryCard key={mem.id} memory={mem} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
cd /home/mihai/tools/open-brain && git add web/src/ && git commit -m "feat(web): implement knowledge graph with Cytoscape.js"
```

---

### Task 13: Build settings page

**Files:**
- Replace: `/home/mihai/tools/open-brain/web/src/app/(dashboard)/settings/page.tsx`

- [ ] **Step 1: Implement settings page**

```tsx
"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Save } from "lucide-react";

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { data: settings, isLoading } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [form, setForm] = useState<Record<string, any>>({});

  useEffect(() => { if (settings) setForm(settings); }, [settings]);

  const mutation = useMutation({
    mutationFn: (s: Record<string, unknown>) => api.saveSettings(s),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings"] }),
  });

  const update = (section: string, key: string, value: any) => {
    setForm((prev) => ({ ...prev, [section]: { ...prev[section], [key]: value } }));
  };

  if (isLoading) return <p className="text-sm text-zinc-500">Loading settings...</p>;

  const db = form.database ?? {};
  const emb = form.embedder ?? {};

  return (
    <div className="max-w-2xl">
      <h1 className="text-xl font-semibold mb-6">Settings</h1>

      <section className="mb-6">
        <h2 className="text-sm font-medium text-zinc-400 mb-3">Database</h2>
        <div className="grid grid-cols-2 gap-3">
          <div><label className="text-[11px] text-zinc-500">Host</label><Input value={db.host ?? ""} onChange={(e) => update("database", "host", e.target.value)} /></div>
          <div><label className="text-[11px] text-zinc-500">Port</label><Input type="number" value={db.port ?? 5432} onChange={(e) => update("database", "port", parseInt(e.target.value))} /></div>
          <div><label className="text-[11px] text-zinc-500">Name</label><Input value={db.name ?? ""} onChange={(e) => update("database", "name", e.target.value)} /></div>
          <div><label className="text-[11px] text-zinc-500">User</label><Input value={db.user ?? ""} onChange={(e) => update("database", "user", e.target.value)} /></div>
        </div>
      </section>

      <section className="mb-6">
        <h2 className="text-sm font-medium text-zinc-400 mb-3">Embedder</h2>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-[11px] text-zinc-500">Provider</label>
            <Select value={emb.provider ?? "ollama"} onValueChange={(v) => update("embedder", "provider", v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="ollama">Ollama</SelectItem>
                <SelectItem value="openrouter">OpenRouter</SelectItem>
                <SelectItem value="openai">OpenAI</SelectItem>
                <SelectItem value="custom">Custom</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div><label className="text-[11px] text-zinc-500">Model</label><Input value={emb.model ?? ""} onChange={(e) => update("embedder", "model", e.target.value)} /></div>
        </div>
      </section>

      <Button onClick={() => mutation.mutate(form)} disabled={mutation.isPending}>
        <Save className="h-4 w-4 mr-2" />{mutation.isPending ? "Saving..." : "Save Settings"}
      </Button>
      {mutation.isSuccess && <p className="mt-2 text-sm text-green-500">Saved! Restart services to apply.</p>}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
cd /home/mihai/tools/open-brain && git add web/src/ && git commit -m "feat(web): implement settings page"
```

---

## Chunk 5: Deployment

### Task 14: Create systemd service and finalize

**Files:**
- Create: `/home/mihai/.config/systemd/user/openbrain-web.service`

- [ ] **Step 1: Build the Next.js production app**

```bash
cd /home/mihai/tools/open-brain/web
npm run build
```

- [ ] **Step 2: Create systemd service**

```bash
cat > ~/.config/systemd/user/openbrain-web.service << 'EOF'
[Unit]
Description=Open Brain Web Dashboard (Next.js)
After=openbrain-api.service

[Service]
WorkingDirectory=/home/mihai/tools/open-brain/web
ExecStart=/run/user/1000/fnm_multishells/1158527_1773422786166/bin/npm run start
Restart=on-failure
Environment=NODE_ENV=production
Environment=PORT=3777

[Install]
WantedBy=default.target
EOF
```

- [ ] **Step 3: Enable and start service**

```bash
systemctl --user daemon-reload
systemctl --user enable openbrain-web.service
systemctl --user start openbrain-web.service
systemctl --user status openbrain-web.service
```

- [ ] **Step 4: Verify dashboard is accessible**

```bash
curl -s http://localhost:3777/search | head -20
```

- [ ] **Step 5: Final commit**

```bash
cd /home/mihai/tools/open-brain
git add web/ docs/
git commit -m "feat: complete Open Brain Next.js dashboard with search, timeline, entities, graph, settings"
```
