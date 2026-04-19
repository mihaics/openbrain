"""
SQL queries for Open Brain memory operations.
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from .connection import get_db_cursor, get_vector_dim


def _validate_embedding_dim(embedding: Optional[List[float]]) -> Optional[List[float]]:
    """Reject embeddings whose length doesn't match the DB column dimension."""
    if embedding is None:
        return None
    expected = get_vector_dim()
    if expected and len(embedding) != expected:
        raise ValueError(
            f"Embedding dimension mismatch: got {len(embedding)}, "
            f"memory.embedding column is vector({expected}). "
            f"Reconfigure the embedder provider/model or run an ALTER TABLE."
        )
    return embedding


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

    embedding = _validate_embedding_dim(embedding)

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
    tags_all: Optional[List[str]] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    importance_min: Optional[float] = None,
    importance_max: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Hybrid search: combines full-text search (PostgreSQL tsvector + ILIKE)
    with semantic vector search (pgvector), ranked by Reciprocal Rank Fusion.

    RRF score = 1/(k_fts + rank_fts) + 1/(k_vec + rank_vector).
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
    if tags_all:
        filters.append("tags @> %s")
        filter_params.append(tags_all)
    if date_from:
        filters.append("created_at >= %s")
        filter_params.append(date_from)
    if date_to:
        filters.append("created_at <= %s")
        filter_params.append(date_to)
    if importance_min is not None:
        filters.append("importance >= %s")
        filter_params.append(float(importance_min))
    if importance_max is not None:
        filters.append("importance <= %s")
        filter_params.append(float(importance_max))

    filter_clause = " AND ".join(filters) if filters else "TRUE"

    has_text = bool(query and query.strip())
    has_vector = embedding is not None
    if has_vector:
        embedding = _validate_embedding_dim(embedding)

    # Candidate pool: fetch more than limit so RRF has enough to merge
    pool = max(limit * 4, 20)

    # --- Hybrid search: both text and vector available ---
    if has_text and has_vector:
        # Candidate CTEs rank inner rows, then the outer select orders by the
        # generated rank before applying LIMIT — without this the planner may
        # pick arbitrary rows. k=1 for text vs k=60 for vector biases toward
        # keyword matches when both signals fire.
        query_sql = f"""
            WITH fts_ranked AS (
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
            ),
            fts AS (
                SELECT id, rank_fts FROM fts_ranked
                ORDER BY rank_fts
                LIMIT %s
            ),
            vec_ranked AS (
                SELECT id,
                    ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) AS rank_vec
                FROM memory
                WHERE ({filter_clause}) AND embedding IS NOT NULL
            ),
            vec AS (
                SELECT id, rank_vec FROM vec_ranked
                ORDER BY rank_vec
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
    """Replace the tag list on a memory, preserving provenance for tags that
    were already present. New tags get source='user'."""
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT tag_sources FROM memory WHERE id = %s",
            (str(memory_id),),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        existing = row['tag_sources'] if isinstance(row, dict) else row[0]
        if isinstance(existing, str):
            existing = json.loads(existing)
        existing = existing or {}

        tag_sources = {t: existing.get(t, 'user') for t in tags}
        cursor.execute(
            "UPDATE memory SET tags = %s, tag_sources = %s WHERE id = %s",
            (tags, json.dumps(tag_sources), str(memory_id)),
        )
        return cursor.rowcount > 0


def update_memory(
    memory_id: uuid.UUID,
    content: Optional[str] = None,
    tags: Optional[List[str]] = None,
    source: Optional[str] = None,
    importance: Optional[float] = None,
    embedding: Optional[List[float]] = None,
    entities: Optional[Dict] = None,
    tag_sources: Optional[Dict] = None,
) -> bool:
    """Partially update a memory. Only fields that are not None are written."""
    sets: List[str] = []
    params: List[Any] = []

    if content is not None:
        sets.append("content = %s")
        params.append(content)
    if tags is not None:
        sets.append("tags = %s")
        params.append(tags)
    if source is not None:
        sets.append("source = %s")
        params.append(source)
    if importance is not None:
        sets.append("importance = %s")
        params.append(float(importance))
    if embedding is not None:
        sets.append("embedding = %s")
        params.append(_validate_embedding_dim(embedding))
    if entities is not None:
        sets.append("entities = %s")
        params.append(json.dumps(entities))
    if tag_sources is not None:
        sets.append("tag_sources = %s")
        params.append(json.dumps(tag_sources))

    if not sets:
        return False

    params.append(str(memory_id))
    with get_db_cursor() as cursor:
        cursor.execute(
            f"UPDATE memory SET {', '.join(sets)} WHERE id = %s",
            tuple(params),
        )
        return cursor.rowcount > 0


def get_related_memories(
    memory_id: uuid.UUID,
    limit: int = 5,
    min_similarity: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Get memories semantically related to the given memory (excluding itself).

    `min_similarity` is applied inside the query so callers always get up to
    `limit` rows above threshold, never a truncated set.
    """
    params: List[Any] = [str(memory_id), str(memory_id)]
    sim_clause = ""
    if min_similarity is not None:
        sim_clause = "AND 1.0 - (m.embedding <=> (SELECT embedding FROM target)) >= %s"
        params.append(float(min_similarity))
    params.append(limit)

    with get_db_cursor() as cursor:
        cursor.execute(f"""
            WITH target AS (
                SELECT embedding FROM memory WHERE id = %s AND embedding IS NOT NULL
            )
            SELECT m.id, m.source, m.source_id, m.content, m.raw_content,
                   m.entities, m.tags, m.tag_sources, m.importance, m.created_at,
                   m.original_date, m.language, m.metadata,
                   1.0 - (m.embedding <=> (SELECT embedding FROM target)) AS score
            FROM memory m, target
            WHERE m.id <> %s AND m.embedding IS NOT NULL
                {sim_clause}
            ORDER BY m.embedding <=> (SELECT embedding FROM target)
            LIMIT %s
        """, tuple(params))
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


def count_memories(
    sources: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    importance_min: Optional[float] = None,
    importance_max: Optional[float] = None,
) -> int:
    """Count memories matching the same filter set as search_memories."""
    filters: List[str] = []
    params: List[Any] = []
    if sources:
        filters.append("source = ANY(%s)")
        params.append(sources)
    if tags:
        filters.append("tags && %s")
        params.append(tags)
    if tags_all:
        filters.append("tags @> %s")
        params.append(tags_all)
    if date_from:
        filters.append("created_at >= %s")
        params.append(date_from)
    if date_to:
        filters.append("created_at <= %s")
        params.append(date_to)
    if importance_min is not None:
        filters.append("importance >= %s")
        params.append(float(importance_min))
    if importance_max is not None:
        filters.append("importance <= %s")
        params.append(float(importance_max))

    where = " AND ".join(filters) if filters else "TRUE"
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS c FROM memory WHERE {where}", tuple(params))
        row = cursor.fetchone()
    return int(row['c'] if isinstance(row, dict) else row[0])


def get_trending_tags(weeks: int = 4, limit: int = 10) -> Dict[str, int]:
    """Get trending tags over the specified number of weeks."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT tag, COUNT(*) as count
            FROM memory, UNNEST(tags) as tag
            WHERE created_at >= CURRENT_DATE - make_interval(weeks => %s)
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

    # Hard cap keeps a pathological date range from pulling the whole table
    # into memory. Per-day trimming is still applied on the Python side below.
    window_days = max((date_to - date_from).days + 1, 1)
    row_cap = min(max(limit_per_day * window_days, 100), 20_000)

    # Single transaction: fetch rows + has_more sentinel together.
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, content, tags, entities, tag_sources,
                   importance, language, created_at, original_date, metadata,
                   created_at::date AS day
            FROM memory
            WHERE created_at >= %s AND created_at <= %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (date_from, date_to, row_cap))
        results = cursor.fetchall()

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
        if isinstance(mem.get('tag_sources'), str):
            mem['tag_sources'] = json.loads(mem['tag_sources'])
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


def find_duplicate_pairs(
    threshold: float = 0.95,
    limit: int = 50,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    sources: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Return memory pairs whose cosine similarity is ≥ threshold.

    Each pair appears once (id_a < id_b) and is sorted by similarity desc.
    Scoped by optional date range / sources so callers can dedupe a slice
    rather than scanning the whole table (HNSW still helps, but pairwise
    joins blow up quadratically without a narrow pool).
    """
    filters: List[str] = ["a.embedding IS NOT NULL", "b.embedding IS NOT NULL", "a.id < b.id"]
    params: List[Any] = []

    def _scope(alias: str) -> None:
        if sources:
            filters.append(f"{alias}.source = ANY(%s)")
            params.append(sources)
        if date_from:
            filters.append(f"{alias}.created_at >= %s")
            params.append(date_from)
        if date_to:
            filters.append(f"{alias}.created_at <= %s")
            params.append(date_to)

    _scope("a")
    _scope("b")

    # Self-join via HNSW is expensive; constrain candidates to the scoped set
    # via a CTE so the planner doesn't try to pair the whole table.
    params.append(float(threshold))
    params.append(int(limit))
    where = " AND ".join(filters)

    with get_db_cursor() as cursor:
        cursor.execute(f"""
            WITH scoped AS (
                SELECT id, embedding, content, source, created_at FROM memory
                WHERE embedding IS NOT NULL
            )
            SELECT a.id AS id_a, b.id AS id_b,
                   1.0 - (a.embedding <=> b.embedding) AS similarity,
                   a.content AS content_a, b.content AS content_b,
                   a.source AS source_a, b.source AS source_b,
                   a.created_at AS created_at_a, b.created_at AS created_at_b
            FROM scoped a JOIN scoped b ON {where}
              AND 1.0 - (a.embedding <=> b.embedding) >= %s
            ORDER BY similarity DESC
            LIMIT %s
        """, tuple(params))
        rows = cursor.fetchall()
    return [dict(r) for r in rows]


def bulk_delete_memories(
    ids: Optional[List[uuid.UUID]] = None,
    sources: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    importance_min: Optional[float] = None,
    importance_max: Optional[float] = None,
) -> int:
    """Delete memories matching the given filters; return rows deleted.

    At least one filter must be provided — callers pass an empty filter set
    at their peril, and the handler enforces a `confirm=True` flag.
    """
    filters: List[str] = []
    params: List[Any] = []
    if ids:
        # Explicit ::uuid[] cast — psycopg2 sends the list as text[], which
        # doesn't have a default operator against memory.id (uuid).
        filters.append("id = ANY(%s::uuid[])")
        params.append([str(i) for i in ids])
    if sources:
        filters.append("source = ANY(%s)")
        params.append(sources)
    if tags:
        filters.append("tags && %s")
        params.append(tags)
    if tags_all:
        filters.append("tags @> %s")
        params.append(tags_all)
    if date_from:
        filters.append("created_at >= %s")
        params.append(date_from)
    if date_to:
        filters.append("created_at <= %s")
        params.append(date_to)
    if importance_min is not None:
        filters.append("importance >= %s")
        params.append(float(importance_min))
    if importance_max is not None:
        filters.append("importance <= %s")
        params.append(float(importance_max))

    if not filters:
        raise ValueError("bulk_delete_memories requires at least one filter")

    where = " AND ".join(filters)
    with get_db_cursor() as cursor:
        cursor.execute(f"DELETE FROM memory WHERE {where}", tuple(params))
        return cursor.rowcount


def get_memories_for_report(
    days: int = 7
) -> List[Dict[str, Any]]:
    """Get all memories from the last N days for report generation."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, source, content, tags, entities, created_at
            FROM memory
            WHERE created_at >= CURRENT_DATE - make_interval(days => %s)
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
