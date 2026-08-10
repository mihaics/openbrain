"""
Search command for CLI.
"""
import json
import sys
from argparse import Namespace

from ..db.connection import get_vector_dim
from ..db.queries import search_memories
from ..embedder import EmbeddingError, create_validated_embedding


def search_memories_cmd(args: Namespace) -> int:
    """Handle the search command."""
    query = args.query
    limit = args.limit
    
    # Generate embedding for semantic search
    embedding = None
    try:
        embedding = create_validated_embedding(query, get_vector_dim())
    except EmbeddingError as e:
        print(f"Warning: text-only search, no query embedding: {e}", file=sys.stderr)
    
    # Build filters
    filters = {}
    if args.source:
        filters['sources'] = [args.source]
    if args.tag:
        filters['tags'] = [args.tag]
    
    # Search
    results = search_memories(
        query=query,
        embedding=embedding,
        explain=getattr(args, 'explain', False),
        limit=limit,
        **filters
    )
    
    # Output. Explain fields live behind the flag and on their own line, so
    # ordinary `--json` output keeps the shape existing consumers expect.
    explain = getattr(args, 'explain', False)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    elif explain:
        if not results:
            print("No memories found.")
            return 0
        for mem in results:
            e = mem.get('explain') or {}
            c = e.get('contribution', {})
            print(f"[{e.get('final', 0):.4f}] {mem.get('content', '')[:64]}")
            print(f"    branch={e.get('branch')} rel_raw={e.get('rel_raw')} "
                  f"rel_norm={e.get('rel_norm', 0):.4f}")
            print(f"    relevance={c.get('relevance', 0):.4f} "
                  f"recency={c.get('recency', 0):.4f} (r={e.get('recency', 0):.3f}) "
                  f"importance={c.get('importance', 0):.4f} "
                  f"access={c.get('access', 0):.4f}")
            print("-" * 40)
    else:
        if not results:
            print("No memories found.")
            return 0
        
        for mem in results:
            print(f"ID: {mem.get('id')}")
            print(f"Source: {mem.get('source')}")
            print(f"Content: {mem.get('content')[:200]}...")
            print(f"Tags: {', '.join(mem.get('tags', []))}")
            print(f"Created: {mem.get('created_at')}")
            print("-" * 40)
    
    return 0
