"""
Open Brain MCP Server.
FastMCP-based MCP server for memory operations.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ToolAnnotations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import connection
from db.connection import get_vector_dim
from db.queries import (
    insert_memory,
    search_memories,
    count_memories,
    get_memory_by_id,
    get_related_memories,
    get_memories_by_entity,
    get_today_memories,
    get_memory_stats,
    update_memory,
    update_memory_tags,
    delete_memory,
    bulk_delete_memories,
    find_duplicate_pairs,
    get_timeline_memories,
    get_entity_graph,
    get_entity_type_counts,
    get_entity_names,
    get_recent_memories,
    get_trending_tags,
)
from embedder import EmbedderConfig, create_embedding
from extractors.entities import extract_entities
from extractors.tagger import auto_tag
from analytics.trends import TrendAnalyzer
from analytics.weekly_report import generate_weekly_report


MEMORY_RECORD_SHAPE = (
    "{id, source, content, tags, entities, importance, created_at, metadata, language?}"
)

ENTITY_TYPES = [
    "people", "organizations", "locations", "technologies", "projects",
    "emails", "urls", "phones", "dates", "hashtags", "mentions",
]

# Shared shape fragment for tools that return memory records; kept here so the
# descriptions stay consistent and easy to update.
CONTENT_ARGS_DOC = {
    "content_mode": {
        "type": "string",
        "enum": ["full", "snippet", "none"],
        "description": "How to return content: 'full' (default), 'snippet' (leading chars), 'none' (omit entirely). Use 'snippet' to keep tool responses compact.",
        "default": "full",
    },
    "content_max_chars": {
        "type": "integer",
        "description": "Cap content length (snippet mode defaults to 240 if unset). Applies to 'full' and 'snippet' modes.",
    },
    "include_tag_sources": {
        "type": "boolean",
        "description": "If true, include `tag_sources` (map of tag → provenance: keyword/pattern/entity/source/user/default).",
        "default": False,
    },
    "include_score": {
        "type": "boolean",
        "description": "If true and the tool computes one (search / get_related), include the ranking `score` on each record.",
        "default": False,
    },
    "include_raw": {
        "type": "boolean",
        "description": "If true, include `raw_content` (original pre-processed text) when stored.",
        "default": False,
    },
}


def _content_opts(args: Dict) -> Dict[str, Any]:
    """Pull the shared content/tag-source options out of a handler's args."""
    mode = args.get("content_mode") or "full"
    if mode not in ("full", "snippet", "none"):
        mode = "full"
    max_chars = args.get("content_max_chars")
    try:
        max_chars = int(max_chars) if max_chars is not None else None
    except (TypeError, ValueError):
        max_chars = None
    return {
        "content_mode": mode,
        "content_max_chars": max_chars,
        "include_tag_sources": bool(args.get("include_tag_sources")),
        "include_score": bool(args.get("include_score")),
        "include_raw": bool(args.get("include_raw")),
    }


def load_config() -> Dict:
    config_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "settings.yaml"
    )
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


CONFIG = load_config()

_DB_READY = False


def init_server() -> None:
    global _DB_READY
    try:
        connection.init_db()
        _DB_READY = True
        print("Database connection initialized", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: Database initialization failed: {e}", file=sys.stderr)
        return

    # Warn loudly if the configured embedder's dimensions don't match the
    # memory.embedding column. A mismatch makes every write fail late (at
    # INSERT time) with an opaque error instead of up front.
    try:
        declared = EmbedderConfig.get_instance().dimensions
        column = get_vector_dim()
        if declared and column and declared != column:
            print(
                f"WARNING: embedder dimensions ({declared}) != "
                f"memory.embedding column ({column}). Every memory_store "
                f"will fail until these agree — update settings.yaml "
                f"'embedder.dimensions' or run an ALTER TABLE on memory.embedding.",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"Warning: could not verify embedder/DB dimension match: {e}", file=sys.stderr)


app = Server("openbrain")


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _clamp(n: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _clamp_importance(v: Any, *, default: float) -> float:
    """Coerce and clamp an importance value into [0, 1]."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))


def _json(payload: Any) -> List[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, default=str, indent=2))]


def _apply_content_mode(
    content: Optional[str],
    mode: str,
    max_chars: Optional[int],
) -> Optional[str]:
    """Trim content per caller's output mode to keep tool responses compact.

    Modes:
      full    — content as stored (capped by max_chars if given)
      snippet — leading max_chars (default 240) with ellipsis suffix if cut
      none    — omit content entirely (returns None)
    """
    if content is None or mode == "full" and max_chars is None:
        return content
    if mode == "none":
        return None
    if mode == "snippet":
        cap = max_chars if max_chars is not None else 240
    else:
        cap = max_chars if max_chars is not None else len(content)
    if len(content) > cap:
        return content[:cap].rstrip() + "…"
    return content


def _serialize_memory(
    mem: Optional[Dict[str, Any]],
    *,
    content_mode: str = "full",
    content_max_chars: Optional[int] = None,
    include_tag_sources: bool = False,
    include_score: bool = False,
    include_raw: bool = False,
) -> Optional[Dict[str, Any]]:
    if mem is None:
        return None
    out = {
        "id": str(mem.get("id")) if mem.get("id") is not None else None,
        "source": mem.get("source"),
        "content": _apply_content_mode(mem.get("content"), content_mode, content_max_chars),
        "tags": mem.get("tags") or [],
        "entities": mem.get("entities") or {},
        "importance": mem.get("importance"),
        "created_at": mem.get("created_at"),
    }
    if mem.get("language"):
        out["language"] = mem["language"]
    if mem.get("metadata"):
        out["metadata"] = mem["metadata"]
    if include_tag_sources and mem.get("tag_sources"):
        out["tag_sources"] = mem["tag_sources"]
    if include_score and mem.get("score") is not None:
        try:
            out["score"] = float(mem["score"])
        except (TypeError, ValueError):
            pass
    if include_raw and mem.get("raw_content"):
        out["raw_content"] = _apply_content_mode(
            mem["raw_content"], content_mode, content_max_chars
        )
    return out


def _serialize_memories(
    memories: List[Dict[str, Any]],
    *,
    content_mode: str = "full",
    content_max_chars: Optional[int] = None,
    include_tag_sources: bool = False,
    include_score: bool = False,
    include_raw: bool = False,
) -> List[Dict[str, Any]]:
    return [
        _serialize_memory(
            m,
            content_mode=content_mode,
            content_max_chars=content_max_chars,
            include_tag_sources=include_tag_sources,
            include_score=include_score,
            include_raw=include_raw,
        )
        for m in memories
        if m is not None
    ]


@app.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="memory_search",
            description=(
                "Hybrid search over memories: combines PostgreSQL full-text "
                "(tsvector + ILIKE) and pgvector semantic similarity, merged "
                "with Reciprocal Rank Fusion. Falls back to text-only or "
                "vector-only when only one signal is available. "
                f"Returns a list of memory records {MEMORY_RECORD_SHAPE}."
            ),
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text search query"},
                    "limit": {"type": "integer", "description": "Max results (1-200, default 10)", "default": 10, "minimum": 1, "maximum": 200},
                    "sources": {"type": "array", "items": {"type": "string"}, "description": "Filter by source values (e.g. claude-code, manual, dashboard, mcp)"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags: OR semantics — memory must contain any of these"},
                    "tags_all": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags: AND semantics — memory must contain all of these"},
                    "date_from": {"type": "string", "description": "From date (ISO 8601, e.g. '2026-04-01' or '2026-04-01T00:00:00')"},
                    "date_to": {"type": "string", "description": "To date (ISO 8601). Hint: for full-day inclusion use 'YYYY-MM-DDT23:59:59'"},
                    "importance_min": {"type": "number", "description": "Minimum importance threshold, 0-1"},
                    "importance_max": {"type": "number", "description": "Maximum importance threshold, 0-1"},
                    **CONTENT_ARGS_DOC,
                },
            },
        ),
        Tool(
            name="memory_store",
            description=(
                "Store a new memory with automatic entity extraction and tagging. "
                "Returns {id, status, tags, entities, embedded}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Content to store"},
                    "source": {"type": "string", "description": "Source label (e.g. claude-code, manual, dashboard, telegram, email, mcp)", "default": "mcp"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional user-supplied tags (merged with auto-tagged)"},
                    "importance": {"type": "number", "description": "Importance 0-1", "default": 0.5},
                    "metadata": {"type": "object", "description": "Additional metadata (stored as-is)"},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="memory_get",
            description=f"Fetch a single memory by ID. Returns a memory record {MEMORY_RECORD_SHAPE} or null.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory"},
                    **CONTENT_ARGS_DOC,
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_update",
            description=(
                "Partially update a memory — only fields you provide are changed. "
                "If 'content' is updated, entities, auto-tags and the embedding are "
                "recomputed automatically (user-supplied 'tags' still win). "
                "Returns {updated: bool, reembedded: bool, tags: [...], entities: {...}}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory"},
                    "content": {"type": "string", "description": "New content; triggers re-embed + re-extract"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Replace tag list (user-supplied)"},
                    "source": {"type": "string"},
                    "importance": {"type": "number", "description": "Importance 0-1"},
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_update_tags",
            description="Replace the tag list on an existing memory. Returns {updated: bool}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "New complete tag list"},
                },
                "required": ["memory_id", "tags"],
            },
        ),
        Tool(
            name="memory_delete",
            description="Delete a memory by ID. Returns {deleted: bool}. Destructive — confirm with user before calling.",
            annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True, readOnlyHint=False),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory to delete"},
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_bulk_delete",
            description=(
                "Delete many memories matching a filter set (same semantics as memory_search). "
                "Destructive — requires `confirm: true` and at least one filter; otherwise errors. "
                "Returns {deleted: int, filters: {...}}."
            ),
            annotations=ToolAnnotations(destructiveHint=True, readOnlyHint=False),
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}, "description": "Specific UUIDs to delete"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "OR semantics"},
                    "tags_all": {"type": "array", "items": {"type": "string"}, "description": "AND semantics"},
                    "date_from": {"type": "string", "description": "ISO 8601"},
                    "date_to": {"type": "string", "description": "ISO 8601"},
                    "importance_min": {"type": "number", "minimum": 0, "maximum": 1},
                    "importance_max": {"type": "number", "minimum": 0, "maximum": 1},
                    "confirm": {"type": "boolean", "description": "Must be true to execute", "default": False},
                },
                "required": ["confirm"],
            },
        ),
        Tool(
            name="memory_find_duplicates",
            description=(
                "Find near-duplicate memory pairs by cosine similarity on embeddings. "
                "Returns [{id_a, id_b, similarity, content_a, content_b, source_a, source_b, "
                "created_at_a, created_at_b}]; each pair appears once (id_a < id_b), "
                "sorted by similarity desc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1, "description": "Minimum cosine similarity (default 0.95)", "default": 0.95},
                    "limit": {"type": "integer", "description": "Max pairs (1-500, default 50)", "default": 50},
                    "sources": {"type": "array", "items": {"type": "string"}, "description": "Only pair memories from these sources"},
                    "date_from": {"type": "string", "description": "ISO 8601"},
                    "date_to": {"type": "string", "description": "ISO 8601"},
                    **{k: CONTENT_ARGS_DOC[k] for k in ("content_mode", "content_max_chars")},
                },
            },
        ),
        Tool(
            name="memory_get_related",
            description=f"Get memories semantically related to a given memory. Returns a list of memory records {MEMORY_RECORD_SHAPE}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory"},
                    "limit": {"type": "integer", "description": "Max results (1-50, default 5)", "default": 5},
                    "min_similarity": {
                        "type": "number",
                        "description": "Drop results with cosine similarity below this threshold (0-1). Default 0 = return all nearest neighbours.",
                    },
                    **CONTENT_ARGS_DOC,
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_get_entity",
            description=f"Get memories containing a specific entity. Returns a list of memory records {MEMORY_RECORD_SHAPE}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "enum": ENTITY_TYPES, "description": "Entity type"},
                    "entity_name": {"type": "string", "description": "Exact entity name"},
                    "limit": {"type": "integer", "description": "Max results (1-200, default 10)", "default": 10, "minimum": 1, "maximum": 200},
                    **CONTENT_ARGS_DOC,
                },
                "required": ["entity_type", "entity_name"],
            },
        ),
        Tool(
            name="memory_today",
            description=(
                "Get memories created today. 'Today' is evaluated against the "
                "PostgreSQL server's CURRENT_DATE — typically the DB container's "
                "timezone, not the caller's. "
                f"Returns a list of memory records {MEMORY_RECORD_SHAPE}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (1-200, default 10)", "default": 10},
                    **CONTENT_ARGS_DOC,
                },
            },
        ),
        Tool(
            name="memory_recent",
            description=f"Get the most recent memories with pagination. Returns a list of memory records {MEMORY_RECORD_SHAPE}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (1-200, default 20)", "default": 20},
                    "offset": {"type": "integer", "description": "Pagination offset (default 0)", "default": 0},
                    "source": {"type": "string", "description": "Optional source filter"},
                    **CONTENT_ARGS_DOC,
                },
            },
        ),
        Tool(
            name="memory_timeline",
            description="Get memories grouped by day for timeline views. Returns {days: {YYYY-MM-DD: [memory, ...]}, has_more: bool}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "From date (ISO 8601)"},
                    "date_to": {"type": "string", "description": "To date (ISO 8601). Use 'YYYY-MM-DDT23:59:59' for full-day inclusion"},
                    "limit_per_day": {"type": "integer", "description": "Max memories per day (1-200, default 50)", "default": 50},
                    **CONTENT_ARGS_DOC,
                },
            },
        ),
        Tool(
            name="memory_graph",
            description="Build the entity co-occurrence graph. Returns {nodes: [{id, label, type, count}], edges: [{source, target, weight}]}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "types": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these entity types"},
                    "limit": {"type": "integer", "description": "Max nodes (1-500, default 200)", "default": 200},
                },
            },
        ),
        Tool(
            name="memory_entity_types",
            description="Get counts of memories per entity type. Returns {entity_type: count}.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="memory_entity_names",
            description="List distinct entity names for a given type, with memory counts. Returns [{name, count}].",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "enum": ENTITY_TYPES, "description": "Entity type"},
                    "limit": {"type": "integer", "description": "Max names (1-500, default 50)", "default": 50, "minimum": 1, "maximum": 500},
                },
                "required": ["entity_type"],
            },
        ),
        Tool(
            name="memory_count",
            description=(
                "Cheap cardinality check — how many memories match the given "
                "filters, without pulling any rows. Same filter semantics as "
                "memory_search. Returns {count: int}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "OR semantics"},
                    "tags_all": {"type": "array", "items": {"type": "string"}, "description": "AND semantics"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "importance_min": {"type": "number"},
                    "importance_max": {"type": "number"},
                },
            },
        ),
        Tool(
            name="memory_stats",
            description="Get memory statistics: total count, per-source counts, top tags, weekly activity. Returns a JSON object.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="memory_trends",
            description="Get trending tags over recent weeks. Returns {tag: count}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "weeks": {"type": "integer", "description": "Look-back window in weeks (1-12, default 4)", "default": 4},
                    "limit": {"type": "integer", "description": "Max tags (1-100, default 10)", "default": 10},
                },
            },
        ),
        Tool(
            name="memory_activity_timeline",
            description="Daily memory counts over the last N days. Returns {YYYY-MM-DD: count}.",
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Look-back window (1-365, default 30)", "default": 30, "minimum": 1, "maximum": 365},
                },
            },
        ),
        Tool(
            name="memory_peak_hours",
            description="Memory counts bucketed by hour-of-day over the last 30 days. Returns {hour: count}.",
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="memory_weekly_report",
            description="Generate a human-readable markdown report of memory activity over the last N days.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Days to include (1-30, default 7)", "default": 7},
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> List[TextContent]:
    if not _DB_READY:
        return _json({"error": "Database not initialized; check server logs."})

    args = arguments if isinstance(arguments, dict) else {}

    try:
        if name == "memory_search":
            return await handle_memory_search(args)
        if name == "memory_store":
            return await handle_memory_store(args)
        if name == "memory_get":
            return await handle_memory_get(args)
        if name == "memory_update":
            return await handle_memory_update(args)
        if name == "memory_update_tags":
            return await handle_memory_update_tags(args)
        if name == "memory_delete":
            return await handle_memory_delete(args)
        if name == "memory_bulk_delete":
            return await handle_memory_bulk_delete(args)
        if name == "memory_find_duplicates":
            return await handle_memory_find_duplicates(args)
        if name == "memory_get_related":
            return await handle_memory_get_related(args)
        if name == "memory_get_entity":
            return await handle_memory_get_entity(args)
        if name == "memory_today":
            return await handle_memory_today(args)
        if name == "memory_recent":
            return await handle_memory_recent(args)
        if name == "memory_timeline":
            return await handle_memory_timeline(args)
        if name == "memory_graph":
            return await handle_memory_graph(args)
        if name == "memory_entity_types":
            return await handle_memory_entity_types(args)
        if name == "memory_entity_names":
            return await handle_memory_entity_names(args)
        if name == "memory_count":
            return await handle_memory_count(args)
        if name == "memory_stats":
            return await handle_memory_stats(args)
        if name == "memory_trends":
            return await handle_memory_trends(args)
        if name == "memory_activity_timeline":
            return await handle_memory_activity_timeline(args)
        if name == "memory_peak_hours":
            return await handle_memory_peak_hours(args)
        if name == "memory_weekly_report":
            return await handle_weekly_report(args)
        return _json({"error": f"Unknown tool: {name}"})
    except ValueError as e:
        return _json({"error": f"Invalid input: {e}"})
    except Exception as e:
        print(f"Tool '{name}' failed: {e}", file=sys.stderr)
        return _json({"error": str(e)})


async def handle_memory_search(args: Dict) -> List[TextContent]:
    query = args.get("query", "") or ""
    limit = _clamp(args.get("limit", 10), 1, 200, 10)
    sources = args.get("sources")
    tags = args.get("tags")
    tags_all = args.get("tags_all")
    date_from = _parse_dt(args.get("date_from"))
    date_to = _parse_dt(args.get("date_to"))

    def _to_float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    importance_min = _to_float(args.get("importance_min"))
    importance_max = _to_float(args.get("importance_max"))

    embedding = None
    if query.strip():
        try:
            embedding = await asyncio.to_thread(create_embedding, query)
        except Exception as e:
            print(f"Warning: Could not create embedding: {e}", file=sys.stderr)

    results = await asyncio.to_thread(
        search_memories,
        query=query,
        embedding=embedding,
        limit=limit,
        sources=sources,
        tags=tags,
        tags_all=tags_all,
        date_from=date_from,
        date_to=date_to,
        importance_min=importance_min,
        importance_max=importance_max,
    )

    return _json(_serialize_memories(results, **_content_opts(args)))


async def handle_memory_store(args: Dict) -> List[TextContent]:
    content = args["content"]
    source = args.get("source", "mcp")
    user_tags = args.get("tags", []) or []
    importance = _clamp_importance(args.get("importance", 0.5), default=0.5)
    metadata = args.get("metadata", {}) or {}

    entities = extract_entities(content)
    tags = auto_tag(content, entities, source, user_tags)

    embedding = None
    embedded = False
    try:
        embedding = await asyncio.to_thread(create_embedding, content)
        embedded = embedding is not None
    except Exception as e:
        print(f"Warning: Could not create embedding: {e}", file=sys.stderr)

    memory_id = await asyncio.to_thread(
        insert_memory,
        source=source,
        content=content,
        embedding=embedding,
        entities=entities,
        tags=list(tags.keys()),
        tag_sources=tags,
        importance=importance,
        metadata=metadata,
    )

    return _json({
        "id": str(memory_id),
        "status": "stored",
        "tags": list(tags.keys()),
        "entities": entities,
        "embedded": embedded,
    })


async def handle_memory_get(args: Dict) -> List[TextContent]:
    memory_id = uuid.UUID(args["memory_id"])
    mem = await asyncio.to_thread(get_memory_by_id, memory_id)
    return _json(_serialize_memory(mem, **_content_opts(args)))


async def handle_memory_update(args: Dict) -> List[TextContent]:
    memory_id = uuid.UUID(args["memory_id"])
    content = args.get("content")
    user_tags = args.get("tags")
    source = args.get("source")
    importance = args.get("importance")

    update_kwargs: Dict[str, Any] = {}
    reembedded = False
    new_entities: Optional[Dict] = None
    new_tags: Optional[List[str]] = None

    if content is not None:
        new_entities = extract_entities(content)
        # Need the stored source to drive source-based tagging when caller
        # didn't override it.
        effective_source = source
        if effective_source is None:
            existing = await asyncio.to_thread(get_memory_by_id, memory_id)
            effective_source = (existing or {}).get("source")
        tag_map = auto_tag(content, new_entities, effective_source, user_tags or [])
        new_tags = list(tag_map.keys())
        update_kwargs["content"] = content
        update_kwargs["entities"] = new_entities
        update_kwargs["tags"] = new_tags
        update_kwargs["tag_sources"] = tag_map
        try:
            emb = await asyncio.to_thread(create_embedding, content)
            if emb:
                update_kwargs["embedding"] = emb
                reembedded = True
        except Exception as e:
            print(f"Warning: re-embed failed on update: {e}", file=sys.stderr)
    elif user_tags is not None:
        update_kwargs["tags"] = user_tags
        update_kwargs["tag_sources"] = {t: "user" for t in user_tags}
        new_tags = user_tags

    if source is not None:
        update_kwargs["source"] = source
    if importance is not None:
        update_kwargs["importance"] = _clamp_importance(importance, default=0.5)

    ok = await asyncio.to_thread(update_memory, memory_id, **update_kwargs)
    return _json({
        "updated": ok,
        "reembedded": reembedded,
        "tags": new_tags,
        "entities": new_entities,
    })


async def handle_memory_update_tags(args: Dict) -> List[TextContent]:
    memory_id = uuid.UUID(args["memory_id"])
    ok = await asyncio.to_thread(update_memory_tags, memory_id, args["tags"])
    return _json({"updated": ok})


async def handle_memory_delete(args: Dict) -> List[TextContent]:
    memory_id = uuid.UUID(args["memory_id"])
    ok = await asyncio.to_thread(delete_memory, memory_id)
    return _json({"deleted": ok})


async def handle_memory_bulk_delete(args: Dict) -> List[TextContent]:
    if not bool(args.get("confirm")):
        return _json({"error": "memory_bulk_delete requires confirm=true"})

    def _to_float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    ids = args.get("ids")
    parsed_ids: Optional[List[uuid.UUID]] = None
    if ids:
        try:
            parsed_ids = [uuid.UUID(i) for i in ids]
        except (TypeError, ValueError) as e:
            return _json({"error": f"Invalid UUID in ids: {e}"})

    try:
        deleted = await asyncio.to_thread(
            bulk_delete_memories,
            ids=parsed_ids,
            sources=args.get("sources"),
            tags=args.get("tags"),
            tags_all=args.get("tags_all"),
            date_from=_parse_dt(args.get("date_from")),
            date_to=_parse_dt(args.get("date_to")),
            importance_min=_to_float(args.get("importance_min")),
            importance_max=_to_float(args.get("importance_max")),
        )
    except ValueError as e:
        return _json({"error": str(e)})

    return _json({
        "deleted": deleted,
        "filters": {
            k: v for k, v in {
                "ids": ids,
                "sources": args.get("sources"),
                "tags": args.get("tags"),
                "tags_all": args.get("tags_all"),
                "date_from": args.get("date_from"),
                "date_to": args.get("date_to"),
                "importance_min": args.get("importance_min"),
                "importance_max": args.get("importance_max"),
            }.items() if v is not None
        },
    })


async def handle_memory_find_duplicates(args: Dict) -> List[TextContent]:
    threshold = args.get("threshold", 0.95)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.95
    threshold = max(0.0, min(1.0, threshold))
    limit = _clamp(args.get("limit", 50), 1, 500, 50)

    pairs = await asyncio.to_thread(
        find_duplicate_pairs,
        threshold=threshold,
        limit=limit,
        date_from=_parse_dt(args.get("date_from")),
        date_to=_parse_dt(args.get("date_to")),
        sources=args.get("sources"),
    )

    opts = _content_opts(args)
    mode, max_chars = opts["content_mode"], opts["content_max_chars"]
    out = []
    for p in pairs:
        out.append({
            "id_a": str(p["id_a"]),
            "id_b": str(p["id_b"]),
            "similarity": float(p["similarity"]),
            "content_a": _apply_content_mode(p.get("content_a"), mode, max_chars),
            "content_b": _apply_content_mode(p.get("content_b"), mode, max_chars),
            "source_a": p.get("source_a"),
            "source_b": p.get("source_b"),
            "created_at_a": p.get("created_at_a"),
            "created_at_b": p.get("created_at_b"),
        })
    return _json(out)


async def handle_memory_get_related(args: Dict) -> List[TextContent]:
    memory_id = uuid.UUID(args["memory_id"])
    limit = _clamp(args.get("limit", 5), 1, 50, 5)
    min_similarity = args.get("min_similarity")
    try:
        min_similarity = float(min_similarity) if min_similarity is not None else None
    except (TypeError, ValueError):
        min_similarity = None
    results = await asyncio.to_thread(
        get_related_memories, memory_id, limit, min_similarity=min_similarity
    )
    return _json(_serialize_memories(results, **_content_opts(args)))


async def handle_memory_get_entity(args: Dict) -> List[TextContent]:
    entity_type = args["entity_type"]
    entity_name = args["entity_name"]
    limit = _clamp(args.get("limit", 10), 1, 200, 10)
    results = await asyncio.to_thread(get_memories_by_entity, entity_type, entity_name, limit)
    return _json(_serialize_memories(results, **_content_opts(args)))


async def handle_memory_today(args: Dict) -> List[TextContent]:
    limit = _clamp(args.get("limit", 10), 1, 200, 10)
    results = await asyncio.to_thread(get_today_memories, limit)
    return _json(_serialize_memories(results, **_content_opts(args)))


async def handle_memory_recent(args: Dict) -> List[TextContent]:
    limit = _clamp(args.get("limit", 20), 1, 200, 20)
    offset = _clamp(args.get("offset", 0), 0, 1_000_000, 0)
    source = args.get("source")
    results = await asyncio.to_thread(get_recent_memories, limit=limit, offset=offset, source=source)
    return _json(_serialize_memories(results, **_content_opts(args)))


async def handle_memory_timeline(args: Dict) -> List[TextContent]:
    date_from = _parse_dt(args.get("date_from"))
    date_to = _parse_dt(args.get("date_to"))
    limit_per_day = _clamp(args.get("limit_per_day", 50), 1, 200, 50)
    result = await asyncio.to_thread(get_timeline_memories, date_from, date_to, limit_per_day)
    opts = _content_opts(args)
    serialized_days = {
        day: _serialize_memories(mems, **opts)
        for day, mems in (result.get("days") or {}).items()
    }
    return _json({"days": serialized_days, "has_more": result.get("has_more", False)})


async def handle_memory_graph(args: Dict) -> List[TextContent]:
    types = args.get("types")
    limit = _clamp(args.get("limit", 200), 1, 500, 200)
    return _json(await asyncio.to_thread(get_entity_graph, types=types, limit=limit))


async def handle_memory_entity_types(args: Dict) -> List[TextContent]:
    return _json(await asyncio.to_thread(get_entity_type_counts))


async def handle_memory_entity_names(args: Dict) -> List[TextContent]:
    entity_type = args["entity_type"]
    limit = _clamp(args.get("limit", 50), 1, 500, 50)
    return _json(await asyncio.to_thread(get_entity_names, entity_type, limit))


async def handle_memory_count(args: Dict) -> List[TextContent]:
    def _to_float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    count = await asyncio.to_thread(
        count_memories,
        sources=args.get("sources"),
        tags=args.get("tags"),
        tags_all=args.get("tags_all"),
        date_from=_parse_dt(args.get("date_from")),
        date_to=_parse_dt(args.get("date_to")),
        importance_min=_to_float(args.get("importance_min")),
        importance_max=_to_float(args.get("importance_max")),
    )
    return _json({"count": count})


async def handle_memory_stats(args: Dict) -> List[TextContent]:
    return _json(await asyncio.to_thread(get_memory_stats))


async def handle_memory_trends(args: Dict) -> List[TextContent]:
    weeks = _clamp(args.get("weeks", 4), 1, 12, 4)
    limit = _clamp(args.get("limit", 10), 1, 100, 10)
    return _json(await asyncio.to_thread(get_trending_tags, weeks=weeks, limit=limit))


async def handle_memory_activity_timeline(args: Dict) -> List[TextContent]:
    days = _clamp(args.get("days", 30), 1, 365, 30)
    analyzer = TrendAnalyzer()
    return _json(await asyncio.to_thread(analyzer.get_activity_timeline, days))


async def handle_memory_peak_hours(args: Dict) -> List[TextContent]:
    analyzer = TrendAnalyzer()
    return _json(await asyncio.to_thread(analyzer.get_peak_activity_hours))


async def handle_weekly_report(args: Dict) -> List[TextContent]:
    days = _clamp(args.get("days", 7), 1, 30, 7)
    report = await asyncio.to_thread(generate_weekly_report, days)
    return [TextContent(type="text", text=report)]


async def main():
    init_server()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
