"""
Open Brain REST API.
FastAPI server for memory operations.
"""
import os
import yaml
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add src to path
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import init_db
from db.queries import (
    search_memories,
    get_memory_by_id,
    insert_memory,
    delete_memory,
    update_memory_tags,
    update_memory,
    get_memory_stats,
    get_today_memories,
    get_recent_memories,
    get_timeline_memories,
    get_entity_type_counts,
    get_entity_names,
    get_entity_memories,
    get_entity_graph
)
from embedder import create_embedding
from extractors.entities import extract_entities
from extractors.tagger import get_tagger
from analytics.trends import TrendAnalyzer
from analytics.weekly_report import generate_weekly_report


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager."""
    # Startup
    try:
        init_db()
    except Exception as e:
        print(f"Warning: Could not initialize database: {e}")
    yield
    # Shutdown


app = FastAPI(
    title="Open Brain API",
    description="REST API for memory management",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Models
class MemoryCreate(BaseModel):
    content: str
    source: str = "api"
    tags: List[str] = []
    importance: float = 0.5
    metadata: dict = {}


class MemoryResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    source: str
    content: str
    tags: List[str]
    entities: dict
    importance: float
    created_at: str

    @classmethod
    def from_memory(cls, mem: dict) -> "MemoryResponse":
        return cls(
            id=str(mem["id"]),
            source=mem["source"],
            content=mem["content"],
            tags=mem.get("tags", []),
            entities=mem.get("entities", {}),
            importance=mem.get("importance", 0.5),
            created_at=str(mem.get("created_at", "")),
        )


class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    sources: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    importance_min: Optional[float] = None


# Routes
@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Open Brain API",
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/memories")
async def get_memories(
    limit: int = Query(50, le=100),
    offset: int = 0,
    source: Optional[str] = None
):
    """Get all memories with pagination."""
    memories = get_recent_memories(limit, offset, source)
    return [MemoryResponse.from_memory(m) for m in memories]


@app.post("/memories", response_model=dict)
async def create_memory(memory: MemoryCreate):
    """Create a new memory."""
    content = memory.content
    source = memory.source
    user_tags = memory.tags
    importance = memory.importance
    metadata = memory.metadata
    
    # Extract entities
    entities = extract_entities(content)
    
    # Auto-tag
    tagger = get_tagger()
    tag_sources = tagger.tag(content, entities, source, user_tags)
    tags = list(tag_sources.keys())
    
    # Generate embedding
    embedding = None
    try:
        embedding = create_embedding(content)
    except Exception as e:
        print(f"Warning: Could not create embedding: {e}")
    
    # Store in database
    memory_id = insert_memory(
        source=source,
        content=content,
        embedding=embedding,
        entities=entities,
        tags=tags,
        tag_sources=tag_sources,
        importance=importance,
        metadata=metadata
    )
    
    return {
        "id": str(memory_id),
        "status": "stored",
        "tags": tags
    }


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


@app.get("/memories/graph")
async def memories_graph(
    types: Optional[str] = None,
    limit: int = Query(200, le=500)
):
    """Get entity co-occurrence graph for visualization."""
    type_list = types.split(",") if types else None
    return get_entity_graph(type_list, limit)


@app.get("/memories/{memory_id}")
async def get_memory(memory_id: str):
    """Get a specific memory by ID."""
    import uuid as uuid_mod
    try:
        mid = uuid_mod.UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    memory = get_memory_by_id(mid)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return MemoryResponse.from_memory(memory)


class MemoryUpdate(BaseModel):
    content: str
    tags: List[str]
    source: str
    importance: float = 0.5


class TagsUpdate(BaseModel):
    tags: List[str]


@app.put("/memories/{memory_id}")
async def update_memory_endpoint(memory_id: str, update: MemoryUpdate):
    """Update a memory."""
    import uuid as uuid_mod
    try:
        mid = uuid_mod.UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    updated = update_memory(mid, update.content, update.tags, update.source, update.importance)
    if not updated:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "updated", "id": memory_id}


@app.patch("/memories/{memory_id}/tags")
async def update_tags_endpoint(memory_id: str, update: TagsUpdate):
    """Update tags for a memory."""
    import uuid as uuid_mod
    try:
        mid = uuid_mod.UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    updated = update_memory_tags(mid, update.tags)
    if not updated:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "updated", "id": memory_id, "tags": update.tags}


@app.delete("/memories/{memory_id}")
async def delete_memory_endpoint(memory_id: str):
    """Delete a memory by ID."""
    import uuid as uuid_mod
    try:
        mid = uuid_mod.UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    deleted = delete_memory(mid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted", "id": memory_id}


@app.post("/memories/search")
async def search_memories_endpoint(search: SearchRequest):
    """Search memories by query."""
    query = search.query
    limit = search.limit
    sources = search.sources
    tags = search.tags

    date_from = None
    date_to = None
    if search.date_from:
        from datetime import datetime
        date_from = datetime.fromisoformat(search.date_from)
    if search.date_to:
        from datetime import datetime
        date_to = datetime.fromisoformat(search.date_to)

    # Generate embedding for semantic search
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

    if search.importance_min is not None:
        results = [m for m in results if m.get("importance", 0) >= search.importance_min]

    return [MemoryResponse.from_memory(m) for m in results]


@app.get("/stats")
async def get_stats():
    """Get memory statistics."""
    stats = get_memory_stats()
    return stats


@app.get("/trends")
async def get_trends(weeks: int = Query(4, le=12)):
    """Get trending topics."""
    analyzer = TrendAnalyzer()
    trends = analyzer.get_trending_topics(weeks)
    return {"trends": trends}


@app.get("/report/weekly")
async def get_weekly_report(days: int = Query(7, le=30)):
    """Generate weekly report."""
    report = generate_weekly_report(days)
    return {"report": report}


SETTINGS_PATH = Path(__file__).parent.parent.parent / "config" / "settings.yaml"


@app.get("/entities/types")
async def entity_types():
    """Get entity type summary with counts."""
    return get_entity_type_counts()


@app.get("/entities/names")
async def entity_names(type: str = Query(...), limit: int = Query(50, le=200)):
    """Get distinct entity names for a type."""
    return get_entity_names(type, limit)


@app.get("/entities/memories")
async def entity_memories_endpoint(
    type: str = Query(...),
    name: str = Query(...),
    limit: int = Query(20, le=100)
):
    """Get memories containing a specific entity."""
    results = get_entity_memories(type, name, limit)
    return [MemoryResponse.from_memory(m) for m in results]


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
