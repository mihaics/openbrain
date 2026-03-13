"""
SQL queries for Open Brain memory operations.
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from .connection import get_db_cursor


def insert_memory(
    source: str,
    content: str,
    embedding: Optional[List[float]] = None,
    source_id: Optional[str] = None,
    raw_content: Optional[str] = None,
    entities: Optional[Dict] = None,
    tags: Optional[List[str]] = None,
    tag_sources: Optional[Dict] = None,
    importance: float = 0.5,
    original_date: Optional[datetime] = None,
    language: Optional[str] = None,
    metadata: Optional[Dict] = None
) -> uuid.UUID:
    """
    Insert a new memory into the database.
    
    Args:
        source: Source of the memory (e.g., 'chat', 'note', 'email')
        content: The main content of the memory
        embedding: Vector embedding for semantic search
        source_id: Optional source-specific ID
        raw_content: Raw/original content before processing
        entities: Extracted entities as JSON
        tags: List of tags
        tag_sources: Source of each tag
        importance: Importance score (0.0 to 1.0)
        original_date: Original date of the memory
        language: Language code
        metadata: Additional metadata
    
    Returns:
        UUID of the inserted memory
    """
    memory_id = uuid.uuid4()
    
    # Ensure JSON fields are proper Python dicts/lists
    entities_dict = dict(entities) if entities else {}
    tags_list = list(tags) if tags else []
    tag_sources_dict = dict(tag_sources) if tag_sources else {}
    metadata_dict = dict(metadata) if metadata else {}
    
    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO memory (
                id, source, source_id, content, raw_content, embedding,
                entities, tags, tag_sources, importance, original_date,
                language, metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """, (
            str(memory_id),
            source,
            source_id,
            content,
            raw_content,
            embedding,
            json.dumps(entities_dict),
            tags_list,
            json.dumps(tag_sources_dict),
            importance,
            original_date,
            language,
            json.dumps(metadata_dict)
        ))
    
    return memory_id


def search_memories(
    query: str,
    embedding: Optional[List[float]] = None,
    limit: int = 5,
    sources: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """
    Hybrid search: combines full-text search (PostgreSQL tsvector + ILIKE)
    with semantic vector search (pgvector), ranked by Reciprocal Rank Fusion.

    RRF score = 1/(k + rank_fts) + 1/(k + rank_vector) where k=60.
    Falls back to text-only or vector-only when one signal is unavailable.
    """
    # Build shared filter conditions
    filters = []
    filter_params = []

    if sources:
        filters.append("source = ANY(%s)")
        filter_params.append(sources)
    if tags:
        filters.append("tags && %s")
        filter_params.append(tags)
    if date_from:
        filters.append("created_at >= %s")
        filter_params.append(date_from)
    if date_to:
        filters.append("created_at <= %s")
        filter_params.append(date_to)

    filter_clause = " AND ".join(filters) if filters else "TRUE"

    has_text = bool(query and query.strip())
    has_vector = embedding is not None

    # Candidate pool: fetch more than limit so RRF has enough to merge
    pool = max(limit * 4, 20)

    # --- Hybrid search: both text and vector available ---
    if has_text and has_vector:
        # FTS CTE: full-text search via tsvector (GIN index) + ILIKE fallback
        # Uses k=1 for text rank (strong boost for keyword matches) vs k=60 for vector
        # This ensures keyword hits always outrank semantic-only matches
        query_sql = f"""
            WITH fts AS (
                SELECT id,
                    ROW_NUMBER() OVER (ORDER BY
                        ts_rank(to_tsvector('english', content), plainto_tsquery('english', %s)) DESC,
                        CASE WHEN content ILIKE %s THEN 0 ELSE 1 END,
                        importance DESC
                    ) AS rank_fts
                FROM memory
                WHERE ({filter_clause})
                    AND (
                        to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                        OR content ILIKE %s
                    )
                LIMIT %s
            ),
            vec AS (
                SELECT id,
                    ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) AS rank_vec
                FROM memory
                WHERE ({filter_clause}) AND embedding IS NOT NULL
                LIMIT %s
            ),
            rrf AS (
                SELECT COALESCE(fts.id, vec.id) AS id,
                    COALESCE(1.0 / (1 + fts.rank_fts), 0) +
                    COALESCE(1.0 / (60 + vec.rank_vec), 0) AS score
                FROM fts
                FULL OUTER JOIN vec ON fts.id = vec.id
                ORDER BY score DESC
                LIMIT %s
            )
            SELECT m.id, m.source, m.source_id, m.content, m.raw_content,
                   m.entities, m.tags, m.tag_sources, m.importance, m.created_at,
                   m.original_date, m.language, m.metadata,
                   rrf.score
            FROM rrf
            JOIN memory m ON m.id = rrf.id
            ORDER BY rrf.score DESC
        """
        like_pattern = f"%{query}%"
        params_final = (
            [query, like_pattern] + filter_params + [query, like_pattern, pool]
            + [embedding] + filter_params + [pool]
            + [limit]
        )

    # --- Text-only fallback (no embedding available) ---
    elif has_text:
        query_sql = f"""
            SELECT id, source, source_id, content, raw_content,
                   entities, tags, tag_sources, importance, created_at,
                   original_date, language, metadata,
                   CASE
                       WHEN to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                       THEN ts_rank(to_tsvector('english', content), plainto_tsquery('english', %s)) + 1.0
                       WHEN content ILIKE %s THEN 0.5
                       ELSE 0.0
                   END AS score
            FROM memory
            WHERE ({filter_clause})
                AND (
                    to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                    OR content ILIKE %s
                )
            ORDER BY score DESC, importance DESC, created_at DESC
            LIMIT %s
        """
        like_pattern = f"%{query}%"
        params_final = (
            [query, query, like_pattern] + filter_params
            + [query, like_pattern, limit]
        )

    # --- Vector-only fallback (no text query) ---
    elif has_vector:
        query_sql = f"""
            SELECT id, source, source_id, content, raw_content,
                   entities, tags, tag_sources, importance, created_at,
                   original_date, language, metadata,
                   1.0 - (embedding <=> %s::vector) AS score
            FROM memory
            WHERE ({filter_clause}) AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        params_final = [embedding] + filter_params + [embedding, limit]

    # --- No search signals, return recent ---
    else:
        query_sql = f"""
            SELECT id, source, source_id, content, raw_content,
                   entities, tags, tag_sources, importance, created_at,
                   original_date, language, metadata,
                   importance AS score
            FROM memory
            WHERE {filter_clause}
            ORDER BY importance DESC, created_at DESC
            LIMIT %s
        """
        params_final = filter_params + [limit]

    with get_db_cursor() as cursor:
        cursor.execute(query_sql, params_final)
        results = cursor.fetchall()

    memories = []
    for row in results:
        mem = dict(row)
        if isinstance(mem.get('entities'), str):
            mem['entities'] = json.loads(mem['entities'])
        if isinstance(mem.get('metadata'), str):
            mem['metadata'] = json.loads(mem['metadata'])
        if isinstance(mem.get('tag_sources'), str):
            mem['tag_sources'] = json.loads(mem['tag_sources'])
        mem['score'] = float(row.get('score', 0))
        memories.append(mem)

    return memories


def get_memory_by_id(memory_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Get a single memory by ID."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, source_id, content, raw_content,
                   entities, tags, tag_sources, importance, created_at,
                   original_date, language, metadata
            FROM memory
            WHERE id = %s
        """, (str(memory_id),))
        row = cursor.fetchone()

    if row:
        mem = dict(row)
        if isinstance(mem.get('entities'), str):
            mem['entities'] = json.loads(mem['entities'])
        if isinstance(mem.get('metadata'), str):
            mem['metadata'] = json.loads(mem['metadata'])
        if isinstance(mem.get('tag_sources'), str):
            mem['tag_sources'] = json.loads(mem['tag_sources'])
        return mem
    return None


def delete_memory(memory_id: uuid.UUID) -> bool:
    """Delete a memory by ID. Returns True if deleted."""
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM memory WHERE id = %s", (str(memory_id),))
        return cursor.rowcount > 0


def update_memory_tags(memory_id: uuid.UUID, tags: List[str]) -> bool:
    """Update tags for a memory. Returns True if updated."""
    with get_db_cursor() as cursor:
        cursor.execute("UPDATE memory SET tags = %s WHERE id = %s", (tags, str(memory_id)))
        return cursor.rowcount > 0


def update_memory(memory_id: uuid.UUID, content: str, tags: List[str], source: str, importance: float) -> bool:
    """Update a memory's content, tags, source and importance. Returns True if updated."""
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE memory SET content = %s, tags = %s, source = %s, importance = %s WHERE id = %s",
            (content, tags, source, importance, str(memory_id))
        )
        return cursor.rowcount > 0


def get_related_memories(
    memory_id: uuid.UUID,
    limit: int = 5
) -> List[Dict[str, Any]]:
    """Get memories related to a given memory."""
    memory = get_memory_by_id(memory_id)
    if not memory or not memory.get('embedding'):
        return []
    
    # Use vector similarity
    return search_memories(
        query="",
        embedding=memory['embedding'],
        limit=limit
    )


def get_memories_by_entity(
    entity_type: str,
    entity_name: str,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """Get memories containing a specific entity."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, source_id, content, raw_content,
                   entities, tags, tag_sources, importance, created_at,
                   original_date, language, metadata
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


def get_today_memories(limit: int = 10) -> List[Dict[str, Any]]:
    """Get memories from today."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, source_id, content, raw_content,
                   entities, tags, tag_sources, importance, created_at,
                   original_date, language, metadata
            FROM memory
            WHERE created_at >= CURRENT_DATE
            ORDER BY importance DESC, created_at DESC
            LIMIT %s
        """, (limit,))
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


def get_memory_stats() -> Dict[str, Any]:
    """Get memory statistics."""
    with get_db_cursor() as cursor:
        # Total count
        cursor.execute("SELECT COUNT(*) as total FROM memory")
        total = cursor.fetchone()['total']
        
        # By source
        cursor.execute("""
            SELECT source, COUNT(*) as count 
            FROM memory 
            GROUP BY source 
            ORDER BY count DESC
        """)
        by_source = {row['source']: row['count'] for row in cursor.fetchall()}
        
        # Top tags
        cursor.execute("""
            SELECT tag, COUNT(*) as count
            FROM memory, UNNEST(tags) as tag
            GROUP BY tag
            ORDER BY count DESC
            LIMIT 20
        """)
        top_tags = {row['tag']: row['count'] for row in cursor.fetchall()}
        
        # This week
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM memory 
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
        """)
        this_week = cursor.fetchone()['count']
        
        # This month
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM memory 
            WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
        """)
        this_month = cursor.fetchone()['count']
    
    return {
        'total': total,
        'by_source': by_source,
        'top_tags': top_tags,
        'this_week': this_week,
        'this_month': this_month
    }


def get_trending_tags(weeks: int = 4, limit: int = 10) -> Dict[str, int]:
    """Get trending tags over the specified number of weeks."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT tag, COUNT(*) as count
            FROM memory, UNNEST(tags) as tag
            WHERE created_at >= CURRENT_DATE - INTERVAL '%s weeks'
            GROUP BY tag
            ORDER BY count DESC
            LIMIT %s
        """, (weeks, limit))
        results = cursor.fetchall()
    
    return {row['tag']: row['count'] for row in results}


def get_recent_memories(
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get recent memories with pagination.
    
    Args:
        limit: Maximum number of results
        offset: Offset for pagination
        source: Optional source filter
    
    Returns:
        List of memory records
    """
    with get_db_cursor() as cursor:
        if source:
            cursor.execute("""
                SELECT id, source, source_id, content, raw_content,
                       entities, tags, tag_sources, importance, created_at,
                       original_date, language, metadata
                FROM memory
                WHERE source = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (source, limit, offset))
        else:
            cursor.execute("""
                SELECT id, source, source_id, content, raw_content,
                       entities, tags, tag_sources, importance, created_at,
                       original_date, language, metadata
                FROM memory
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
        
        results = cursor.fetchall()
    
    memories = []
    for row in results:
        mem = dict(row)
        if isinstance(mem.get('entities'), str):
            mem['entities'] = json.loads(mem['entities'])
        if isinstance(mem.get('metadata'), str):
            mem['metadata'] = json.loads(mem['metadata'])
        if isinstance(mem.get('tag_sources'), str):
            mem['tag_sources'] = json.loads(mem['tag_sources'])
        memories.append(mem)
    
    return memories


def get_timeline_memories(
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit_per_day: int = 50
) -> Dict[str, Any]:
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

    has_more = False
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM memory WHERE created_at < %s) AS has_more",
            (date_from,)
        )
        has_more = cursor.fetchone()['has_more']

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


def get_entity_graph(
    types: Optional[List[str]] = None,
    limit: int = 200
) -> Dict[str, Any]:
    """Build entity co-occurrence graph for Cytoscape.js."""
    from collections import defaultdict

    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, entities FROM memory WHERE entities != '{}'::jsonb")
        rows = cursor.fetchall()

    entity_counts: Dict[str, int] = defaultdict(int)
    edge_counts: Dict[tuple, int] = defaultdict(int)

    for row in rows:
        entities_data = row['entities']
        if isinstance(entities_data, str):
            entities_data = json.loads(entities_data)

        memory_entities = []
        for etype, names in entities_data.items():
            if types and etype not in types:
                continue
            if isinstance(names, list):
                for name in names:
                    node_id = f"{etype}:{name}"
                    entity_counts[node_id] += 1
                    memory_entities.append((node_id, name, etype))

        for i, (id_a, _, _) in enumerate(memory_entities):
            for j in range(i + 1, len(memory_entities)):
                id_b = memory_entities[j][0]
                if id_a != id_b:
                    edge_key = tuple(sorted([id_a, id_b]))
                    edge_counts[edge_key] += 1

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


def get_memories_for_report(
    days: int = 7
) -> List[Dict[str, Any]]:
    """Get all memories from the last N days for report generation."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, content, tags, entities, created_at
            FROM memory
            WHERE created_at >= CURRENT_DATE - INTERVAL '%s days'
            ORDER BY created_at DESC
        """, (days,))
        results = cursor.fetchall()
    
    memories = []
    for row in results:
        mem = dict(row)
        if isinstance(mem.get('entities'), str):
            mem['entities'] = json.loads(mem['entities'])
        memories.append(mem)
    
    return memories
