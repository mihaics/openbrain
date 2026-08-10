"""Optional reranking seam.

Disabled by default and degrading to the SQL order on every failure path.
Ollama has no rerank endpoint, which is why this is configurable rather than
assumed: it is a seam for later, not a feature being claimed now.
"""
import json
import math
import sys
from typing import Any, Dict, List, Optional

import requests

MAX_TOP_N = 200
MAX_REQUEST_BYTES = 1_000_000
MAX_DOCUMENT_BYTES = 4_000


def _valid_results(payload: Any, document_count: int) -> Optional[List[Dict]]:
    """Validate a rerank response, or reject it wholesale.

    Partial trust is not useful here: a response we half-understand would
    reorder results by a rule nobody can state. Any defect falls back to the
    SQL order, which is always correct if not always optimal.
    """
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or len(results) > document_count:
        return None

    seen = set()
    for entry in results:
        if not isinstance(entry, dict):
            return None
        index, score = entry.get("index"), entry.get("relevance_score")
        if not isinstance(index, int) or not 0 <= index < document_count:
            return None
        if index in seen:
            return None
        seen.add(index)
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return None
        if not math.isfinite(score):
            return None
    return results


def rerank(query: str, candidates: List[Dict[str, Any]], cfg) -> List[Dict[str, Any]]:
    """Reorder candidates by an external reranker, or return them unchanged.

    Bypassed when there is no text query: the vector-only and no-signal
    branches have nothing meaningful to put in the request, and reranking a
    filter-only listing against an empty string would reorder it arbitrarily.
    """
    if not cfg.rerank_enabled or not (query or "").strip() or not candidates:
        return candidates

    top_n = min(cfg.rerank_top_n, MAX_TOP_N, len(candidates))
    head, tail = candidates[:top_n], candidates[top_n:]
    documents = [(c.get("content") or "")[:MAX_DOCUMENT_BYTES] for c in head]

    body = {"model": cfg.rerank_model, "query": query,
            "documents": documents, "top_n": top_n}
    if len(json.dumps(body)) > MAX_REQUEST_BYTES:
        print("Warning: rerank request too large, skipping", file=sys.stderr)
        return candidates

    headers = {"Content-Type": "application/json"}
    if cfg.rerank_api_key:
        headers["Authorization"] = f"Bearer {cfg.rerank_api_key}"

    try:
        response = requests.post(cfg.rerank_url, json=body, headers=headers,
                                 timeout=cfg.rerank_timeout_seconds)
        response.raise_for_status()
        results = _valid_results(response.json(), len(documents))
    except Exception as e:
        print(f"Warning: rerank unavailable, keeping SQL order: {e}", file=sys.stderr)
        return candidates

    if results is None:
        print("Warning: malformed rerank response, keeping SQL order", file=sys.stderr)
        return candidates

    # Score is authoritative, not response array order - a conforming endpoint
    # may return results unsorted. The original position breaks ties so the
    # outcome is deterministic.
    ordered = sorted(results, key=lambda r: (-r["relevance_score"], r["index"]))
    reranked = []
    for entry in ordered:
        item = dict(head[entry["index"]])
        item["rerank_score"] = entry["relevance_score"]
        reranked.append(item)

    # An endpoint returns at most top_n results and is not required to return
    # one per document. Whatever it omitted keeps its SQL order, appended.
    returned = {e["index"] for e in ordered}
    leftovers = [head[i] for i in range(len(head)) if i not in returned]

    return reranked + leftovers + tail
