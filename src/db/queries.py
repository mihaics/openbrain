"""
SQL queries for Open Brain memory operations.
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .connection import get_db_cursor, get_vector_dim

# This module is loaded under two identities: `src.db.queries` by the CLI and
# tests, and `db.queries` by src/main.py and src/api/main.py, which put src/ on
# sys.path first. Under the latter the parent package is `db`, so a `..config`
# relative import reaches beyond the top-level package and fails. Both spellings
# are tried rather than "fixing" the repo's mixed import style here.
try:
    from ..config.ranking import RankingConfig
except ImportError:  # pragma: no cover - exercised by the MCP/API import path
    from config.ranking import RankingConfig


JSON_MEMORY_FIELDS = ("entities", "metadata", "tag_sources")


UNSET = object()
"""Sentinel distinguishing "field not supplied" from an explicit None.

update_memory previously treated None as "do not update this field", which
made it impossible to clear an embedding. Callers that must clear one now
pass embedding=None; callers leaving it alone pass nothing.
"""


def _decode_json_field(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _decode_memory_row(row: Any) -> Dict[str, Any]:
    """Normalize DB/mocked rows into a consistent memory-record dict."""
    mem = dict(row)
    for field in JSON_MEMORY_FIELDS:
        if field in mem:
            mem[field] = _decode_json_field(mem.get(field)) or {}
    return mem


def _decode_memory_rows(rows: List[Any]) -> List[Dict[str, Any]]:
    return [_decode_memory_row(row) for row in rows]


def _validate_embedding(embedding: Optional[List[float]]) -> Optional[List[float]]:
    """Reject embeddings that would corrupt vector search. None passes through.

    This is a backstop. Callers should validate via
    embedder.create_validated_embedding inside their own try block, because
    this function raises after their exception handling has finished.
    """
    if embedding is None:
        return None

    if all(v == 0.0 for v in embedding):
        raise ValueError(
            "Refusing to store an all-zero embedding: cosine distance "
            "against it is NaN, which corrupts ranking and duplicate "
            "detection. Store NULL instead."
        )

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

    embedding = _validate_embedding(embedding)

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


def _like_pattern(query: str) -> str:
    """Build an ILIKE pattern with metacharacters escaped.

    Unescaped, a query of '%' is a match-all pattern and '_' matches any
    character. The backslash must be escaped first or it would double-escape
    the escapes added after it.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# plainto_tsquery stems and drops stopwords, then ANDs what remains - so one
# unmatched word returns nothing, which is why the lexical arm contributes
# nothing to natural-language queries. Relaxing the conjunction keeps all of
# PostgreSQL's normalisation and makes the arm actually fire. The value stays
# a bound parameter; the cast happens server-side on an already-normalised
# tsquery, so no injection surface is introduced.
_TSQUERY_EXPR = "replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery"


# Blending fragments. Every input is clamped in SQL because none is
# guaranteed by the schema: importance is nullable and unconstrained,
# created_at can be NULL or future-dated, access_count is an integer.
_RECENCY_EXPR = """CASE WHEN m.created_at IS NULL THEN 0
             ELSE exp(-ln(2) * GREATEST(EXTRACT(EPOCH FROM (NOW() - m.created_at)) / 86400.0, 0) / %s)
        END"""
# GREATEST(NULL, 0) is 0 in PostgreSQL, so without the explicit NULL case an
# undated memory would score exp(0) = 1.0: maximally recent.
_IMPORTANCE_EXPR = "LEAST(GREATEST(COALESCE(m.importance, 0.5), 0), 1)"
# LEAST(7,10)/10 is integer division and evaluates to 0, hence the cast.
_ACCESS_EXPR = ("LEAST(GREATEST(COALESCE(m.access_count, 0), 0), %s)"
                "::double precision / %s")


def _blend_select(rel_expr: str) -> str:
    """Score = normalized relevance plus bounded secondary signals.

    rel_norm is COALESCE'd because an all-zero relevance pool - every cosine
    similarity clamped to zero, say - makes MAX() zero, every ratio NULL and
    every score NULL, which silently falls through to timestamp ordering.
    """
    return f"""COALESCE({rel_expr} / NULLIF(MAX({rel_expr}) OVER (), 0), 0)
                   + %s * ({_RECENCY_EXPR})
                   + %s * ({_IMPORTANCE_EXPR})
                   + %s * ({_ACCESS_EXPR})"""


def _blend_params(cfg) -> list:
    """Parameters for _blend_select, in the order they appear."""
    return [cfg.w_rec, cfg.half_life_days, cfg.w_imp,
            cfg.w_acc, cfg.access_cap, cfg.access_cap]


MEMORY_COLUMNS = """m.id, m.source, m.source_id, m.content, m.raw_content,
                   m.entities, m.tags, m.tag_sources, m.importance, m.created_at,
                   m.original_date, m.language, m.metadata"""


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
    explain: bool = False,
) -> List[Dict[str, Any]]:
    """
    Hybrid search: PostgreSQL full-text OR-of-lexemes fused with pgvector
    similarity by Reciprocal Rank Fusion, then ordered.

    Both arms use k=60. Earlier code used k=1 for text and k=60 for vector,
    which capped the best semantic match at 0.0164 - below the twentieth-best
    lexical match at 0.0476 - so vector similarity could never promote a row,
    only break ties among rows the keyword arm had already found.

    The caller's LIMIT is applied exactly once, after ordering. Applying it
    inside the fusion CTE (as earlier code did) means nothing downstream can
    promote a candidate from the wider pool.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be an integer >= 1, got {limit!r}")

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
        embedding = _validate_embedding(embedding)

    cfg = RankingConfig.get_instance()
    pool = max(limit * 4, 20)

    if has_text and has_vector:
        query_sql = f"""
            WITH tsq AS (SELECT {_TSQUERY_EXPR} AS q),
            fts AS (
                SELECT m.id,
                    ROW_NUMBER() OVER (
                        ORDER BY ts_rank_cd(to_tsvector('english', m.content), tsq.q) DESC,
                                 m.id
                    ) AS rank_fts
                FROM memory m, tsq
                WHERE ({filter_clause})
                  AND tsq.q != ''::tsquery
                  AND (to_tsvector('english', m.content) @@ tsq.q
                       OR m.content ILIKE %s ESCAPE '\\')
                ORDER BY rank_fts
                LIMIT %s
            ),
            vec AS (
                SELECT m.id,
                    ROW_NUMBER() OVER (ORDER BY m.embedding <=> %s::vector, m.id) AS rank_vec
                FROM memory m
                WHERE ({filter_clause}) AND m.embedding IS NOT NULL
                ORDER BY rank_vec
                LIMIT %s
            ),
            fused AS (
                SELECT COALESCE(fts.id, vec.id) AS id,
                       COALESCE(1.0 / (60 + fts.rank_fts), 0) +
                       COALESCE(1.0 / (60 + vec.rank_vec), 0) AS rel_raw
                FROM fts FULL OUTER JOIN vec ON fts.id = vec.id
            )
            SELECT {MEMORY_COLUMNS},
                   {_blend_select('fused.rel_raw')} AS score,
                   fused.rel_raw AS rel_raw,
                   COALESCE(fused.rel_raw / NULLIF(MAX(fused.rel_raw) OVER (), 0), 0) AS rel_norm
            FROM fused JOIN memory m ON m.id = fused.id
            ORDER BY score DESC NULLS LAST,
                     m.created_at DESC NULLS LAST, m.id
            LIMIT %s
        """
        like = _like_pattern(query)
        # Parameter order follows the order placeholders appear in the SQL
        # text: the tsquery CTE, then each arm, then the outer SELECT's blend
        # terms, then the final limit.
        params_final = (
            [query] + filter_params + [like, pool]
            + [embedding] + filter_params + [pool]
            + _blend_params(cfg)
            + [limit]
        )

    elif has_text:
        query_sql = f"""
            WITH tsq AS (SELECT {_TSQUERY_EXPR} AS q)
            SELECT {MEMORY_COLUMNS},
                   {_blend_select("ts_rank_cd(to_tsvector('english', m.content), tsq.q)")} AS score,
                   ts_rank_cd(to_tsvector('english', m.content), tsq.q) AS rel_raw,
                   COALESCE(ts_rank_cd(to_tsvector('english', m.content), tsq.q)
                            / NULLIF(MAX(ts_rank_cd(to_tsvector('english', m.content), tsq.q)) OVER (), 0), 0) AS rel_norm
            FROM memory m, tsq
            WHERE ({filter_clause})
              AND tsq.q != ''::tsquery
              AND (to_tsvector('english', m.content) @@ tsq.q
                   OR m.content ILIKE %s ESCAPE '\\')
            ORDER BY score DESC NULLS LAST,
                     m.importance DESC NULLS LAST,
                     m.created_at DESC NULLS LAST, m.id
            LIMIT %s
        """
        params_final = ([query] + _blend_params(cfg) + filter_params
                        + [_like_pattern(query), limit])

    elif has_vector:
        query_sql = f"""
            SELECT {MEMORY_COLUMNS},
                   {_blend_select('GREATEST(1.0 - (m.embedding <=> %s::vector), 0)')} AS score
            FROM memory m
            WHERE ({filter_clause}) AND m.embedding IS NOT NULL
            ORDER BY score DESC NULLS LAST, m.created_at DESC NULLS LAST, m.id
            LIMIT %s
        """
        # rel_expr appears twice in _blend_select (the value and the MAX
        # window), so the embedding parameter is supplied twice, and both
        # precede the blend terms in the SQL text.
        params_final = ([embedding, embedding] + _blend_params(cfg)
                        + filter_params + [limit])

    else:
        query_sql = f"""
            SELECT {MEMORY_COLUMNS}, m.importance AS score
            FROM memory m
            WHERE ({filter_clause})
            ORDER BY m.importance DESC NULLS LAST,
                     m.created_at DESC NULLS LAST, m.id
            LIMIT %s
        """
        params_final = filter_params + [limit]

    with get_db_cursor() as cursor:
        cursor.execute(query_sql, params_final)
        results = cursor.fetchall()

    branch = ("hybrid" if (has_text and has_vector)
              else "text-only" if has_text
              else "vector-only" if has_vector else "no-signal")

    memories = []
    for row in results:
        mem = _decode_memory_row(row)
        mem['score'] = float(row.get('score') or 0)
        if explain:
            mem['explain'] = _explain_components(row, cfg, branch)
        memories.append(mem)

    return memories


def _explain_components(row: Any, cfg, branch: str) -> Dict[str, Any]:
    """Per-result score breakdown.

    Secondary components are recomputed here from the row's own fields rather
    than selected again in SQL: the inputs are already returned, the formula
    is fixed, and repeating the expressions would add placeholders whose
    ordering is exactly what broke the first blended query.
    """
    import math

    created = row.get("created_at")
    if created is None:
        recency = 0.0
    else:
        age_days = max((datetime.now(created.tzinfo) - created).total_seconds() / 86400.0, 0)
        recency = math.exp(-math.log(2) * age_days / cfg.half_life_days)

    importance = min(max(row.get("importance") if row.get("importance") is not None else 0.5, 0), 1)
    access = min(max(row.get("access_count") or 0, 0), cfg.access_cap) / cfg.access_cap
    rel_norm = float(row.get("rel_norm") or 0)

    return {
        "branch": branch,
        "rel_raw": float(row.get("rel_raw") or 0) if row.get("rel_raw") is not None else None,
        "rel_norm": rel_norm,
        "recency": recency,
        "importance": float(importance),
        "access": access,
        "contribution": {
            "relevance": rel_norm,
            "recency": cfg.w_rec * recency,
            "importance": cfg.w_imp * float(importance),
            "access": cfg.w_acc * access,
        },
        "final": float(row.get("score") or 0),
    }


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
        return _decode_memory_row(row)
    return None


def delete_memory(memory_id: uuid.UUID) -> bool:
    """Delete a memory by ID. Returns True if deleted."""
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM memory WHERE id = %s", (str(memory_id),))
        return cursor.rowcount > 0


def update_memory_tags(memory_id: uuid.UUID, tags: List[str]) -> bool:
    """Replace the tag list on a memory. Every submitted tag becomes user-owned."""
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

        # Explicit submission is user intent. Keeping an old automatic
        # provenance here launders a protected tag into a removable one: the
        # retag backfill would then be entitled to delete a tag the user
        # deliberately chose to keep.
        tag_sources = {t: 'user' for t in tags}
        cursor.execute(
            "UPDATE memory SET tags = %s, tag_sources = %s WHERE id = %s",
            (tags, json.dumps(tag_sources), str(memory_id)),
        )
        return cursor.rowcount > 0


PROTECTED_PROVENANCE = ("user",)


def merge_tags_for_content_update(
    memory_id: uuid.UUID,
    recomputed: Dict[str, str],
) -> Tuple[List[str], Dict[str, str]]:
    """Merge recomputed automatic tags over the tags a user owns.

    Recomputing the whole tag set on a content update destroys every
    hand-added tag, because the tagger only knows what it can derive from the
    text. Tags whose provenance is `user` - or whose provenance is missing,
    which is how rows predating provenance tracking look - are retained.

    Protected provenance wins every collision: a stored `python: user` stays
    `user` even when the recomputation would emit `python: keyword`.
    Downgrading it would make the tag removable by the retag backfill.

    Read and write happen in one transaction under a row lock, or a
    concurrent explicit tag update can be read here and overwritten by the
    stale set computed from it.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT tags, tag_sources FROM memory WHERE id = %s FOR UPDATE",
            (str(memory_id),),
        )
        row = cursor.fetchone()
        if row is None:
            return list(recomputed), dict(recomputed)

        stored_tags = (row['tags'] if isinstance(row, dict) else row[0]) or []
        stored_sources = (row['tag_sources'] if isinstance(row, dict) else row[1]) or {}
        if isinstance(stored_sources, str):
            stored_sources = json.loads(stored_sources)

    merged: Dict[str, str] = dict(recomputed)
    for tag in stored_tags:
        provenance = stored_sources.get(tag)
        # Missing provenance fails closed: it may be a user tag from before
        # provenance was tracked, and losing one is worse than keeping noise.
        if provenance is None or provenance in PROTECTED_PROVENANCE:
            merged[tag] = 'user'

    return list(merged), merged


def update_memory(
    memory_id: uuid.UUID,
    content: Optional[str] = None,
    tags: Optional[List[str]] = None,
    source: Optional[str] = None,
    importance: Optional[float] = None,
    embedding: Any = UNSET,
    entities: Optional[Dict] = None,
    tag_sources: Optional[Dict] = None,
) -> bool:
    """Partially update a memory. Only supplied fields are written.

    embedding is three-valued: UNSET leaves the column alone, None sets it
    to NULL, a list replaces it.

    Raises:
        ValueError: content supplied without an embedding decision. Writing
            new content over an old vector leaves the row semantically
            mismatched, findable by a query that matches text it no longer
            contains.
    """
    if content is not None and embedding is UNSET:
        raise ValueError(
            "update_memory: content was supplied without an embedding "
            "decision. Pass a fresh embedding, or embedding=None to clear "
            "the stale one. Leaving the old vector attached to new content "
            "silently corrupts vector search."
        )

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
    if embedding is not UNSET:
        if embedding is None:
            sets.append("embedding = NULL")
        else:
            sets.append("embedding = %s")
            params.append(_validate_embedding(embedding))
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
        mem = _decode_memory_row(row)
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
    
    return _decode_memory_rows(results)


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
    
    return _decode_memory_rows(results)


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
    
    return _decode_memory_rows(results)


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
        mem = _decode_memory_row(row)
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

    return _decode_memory_rows(results)


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
    
    return _decode_memory_rows(results)
