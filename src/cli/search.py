"""
Search command for CLI.
"""
import json
import sys
from argparse import Namespace

from ..db.queries import search_memories
from ..embedder import create_embedding


def search_memories_cmd(args: Namespace) -> int:
    """Handle the search command."""
    query = args.query
    limit = args.limit
    
    # Generate embedding for semantic search
    embedding = None
    try:
        embedding = create_embedding(query)
    except Exception as e:
        print(f"Warning: Could not create embedding: {e}", file=sys.stderr)
    
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
        limit=limit,
        **filters
    )
    
    # Output
    if args.json:
        print(json.dumps(results, indent=2, default=str))
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
