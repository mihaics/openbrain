"""Storing captured facts: one transaction per boundary, or nothing.

The generation transition happens inside the same transaction as the rows.
Updating capture_state afterwards from the worker would recreate exactly the
crash window the restore interlock exists to close.
"""
import hashlib
import json
import re
import uuid as uuid_mod
from typing import Any, Dict, List, Optional, Tuple

try:
    from ..db.connection import get_db_cursor, get_vector_dim
    from ..embedder import EmbeddingError, create_validated_embedding
except ImportError:  # pragma: no cover - MCP/API import path
    from db.connection import get_db_cursor, get_vector_dim
    from embedder import EmbeddingError, create_validated_embedding

AUTO_SOURCE = "claude-code-auto"
FIXED_IMPORTANCE = 0.5


class StoreOutcome:
    STORED = "stored"
    NOTHING_TO_DO = "nothing"
    EMBEDDING_FAILURE = "embedding_failure"
    DB_FAILURE = "db_failure"
    ALREADY_COMMITTED = "already_committed"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def source_id_for(work_identity: Dict[str, Any], fact: str) -> str:
    """Deterministic per fact.

    Per-fact, or three facts from one boundary collapse under a single id.
    Versioned, or a reprocess collides with the original run it is meant to
    supersede.
    """
    raw = "|".join([
        work_identity.get("boundary_id", ""),
        str(work_identity.get("prompt_version", "")),
        str(work_identity.get("model_digest", "")),
        str(work_identity.get("reprocess_generation", 0)),
        normalize(fact),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


def boundary_already_committed(boundary_id: str) -> bool:
    """Has any row from this boundary already landed?

    Checked before invoking the model, so a crash between commit and progress
    costs nothing but a lookup.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM memory
             WHERE source = %s
               AND metadata->>'boundary_id' = %s
             LIMIT 1
            """,
            (AUTO_SOURCE, boundary_id),
        )
        return cursor.fetchone() is not None


def daily_count() -> int:
    """Committed rows today, by the database clock in UTC.

    memory.created_at is `timestamp without time zone`, so it stores the
    connection's wall clock; capture writes force UTC (below) and this counts
    in the same frame. A local day boundary would duplicate or skip an hour
    twice a year under Europe/Bucharest DST.
    """
    with get_db_cursor() as cursor:
        cursor.execute("SET LOCAL TIME ZONE 'UTC'")
        cursor.execute(
            """
            SELECT count(*) AS n FROM memory
             WHERE source = %s
               AND created_at >= date_trunc('day', now())
            """,
            (AUTO_SOURCE,),
        )
        row = cursor.fetchone()
    return row["n"] if isinstance(row, dict) else row[0]


def find_duplicate(fact: str, embedding: Optional[List[float]],
                   threshold: float) -> Optional[Tuple[str, float]]:
    """Exact hash first, then an exhaustive nearest scan.

    Hash first because it also catches rows whose embedding is NULL, which a
    vector search cannot see at all. Exhaustive rather than HNSW because at
    this scale approximate search need not return the true nearest neighbour.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id FROM memory
             WHERE md5(regexp_replace(lower(trim(content)), '\\s+', ' ', 'g'))
                 = md5(%s)
             LIMIT 1
            """,
            (normalize(fact),),
        )
        exact = cursor.fetchone()
        if exact:
            return (str(exact["id"] if isinstance(exact, dict) else exact[0]), 1.0)

        if embedding is None:
            return None

        cursor.execute(
            """
            SELECT id, content, 1 - (embedding <=> %s::vector) AS similarity
              FROM memory
             WHERE embedding IS NOT NULL
             ORDER BY embedding <=> %s::vector
             LIMIT 1
            """,
            (embedding, embedding),
        )
        row = cursor.fetchone()

    if not row:
        return None
    similarity = float(row["similarity"] if isinstance(row, dict) else row[2])
    if similarity >= threshold:
        return (str(row["id"] if isinstance(row, dict) else row[0]), similarity)
    return None


def store_boundary(facts: List[Dict[str, str]], work_identity: Dict[str, Any],
                   metadata: Dict[str, Any], cfg,
                   expected_generation: int) -> Tuple[str, Dict[str, Any]]:
    """Insert every fact and advance the generation, or insert nothing.

    Embeddings are generated and validated *before* the transaction opens: a
    failure partway through would otherwise leave half a boundary committed.
    """
    report = {"stored": 0, "duplicates": 0, "candidates": len(facts)}
    if not facts:
        return StoreOutcome.NOTHING_TO_DO, report

    # --- outside the transaction ---
    prepared = []
    try:
        dim = get_vector_dim()
        for item in facts:
            embedding = create_validated_embedding(item["fact"], dim)
            prepared.append((item, embedding))
    except EmbeddingError as e:
        report["detail"] = str(e)
        return StoreOutcome.EMBEDDING_FAILURE, report

    seen_hashes = set()
    final = []
    for item, embedding in prepared:
        h = content_hash(item["fact"])
        if h in seen_hashes:
            report["duplicates"] += 1
            continue
        seen_hashes.add(h)
        dup = find_duplicate(item["fact"], embedding, cfg.dedup_threshold)
        if dup:
            report["duplicates"] += 1
            continue
        final.append((item, embedding))

    if not final:
        return StoreOutcome.NOTHING_TO_DO, report

    # --- one transaction: rows and the generation transition together ---
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute(
                "SELECT generation FROM capture_state WHERE id = 1 FOR UPDATE"
            )
            row = cursor.fetchone()
            current = row["generation"] if isinstance(row, dict) else row[0]
            if current != expected_generation:
                return StoreOutcome.DB_FAILURE, dict(
                    report, detail=f"generation moved: {current} != {expected_generation}")

            for item, embedding in final:
                sid = source_id_for(work_identity, item["fact"])
                cursor.execute(
                    "SELECT 1 FROM memory WHERE source_id = %s AND source = %s",
                    (sid, AUTO_SOURCE),
                )
                if cursor.fetchone():
                    report["duplicates"] += 1
                    continue

                cursor.execute(
                    """
                    INSERT INTO memory (id, source, source_id, content, embedding,
                                        entities, tags, tag_sources, importance,
                                        metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (str(uuid_mod.uuid4()), AUTO_SOURCE, sid, item["fact"],
                     embedding, json.dumps({}), [], json.dumps({}),
                     FIXED_IMPORTANCE,
                     json.dumps({**metadata, **work_identity,
                                 "category": item.get("category", "note")})),
                )
                report["stored"] += 1

            cursor.execute(
                """
                UPDATE capture_state
                   SET previous_generation = generation,
                       generation = generation + 1,
                       committed_work_identity = %s,
                       updated_at = now()
                 WHERE id = 1
                """,
                (json.dumps(work_identity),),
            )
    except Exception as e:
        report["detail"] = str(e)
        return StoreOutcome.DB_FAILURE, report

    return StoreOutcome.STORED, report
