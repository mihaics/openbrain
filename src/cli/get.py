"""Fetch a single memory by id, and count it as an access."""
import json
import sys
from argparse import Namespace

from ..db.queries import get_memory_by_id, record_access


def get_memory_cmd(args: Namespace) -> int:
    """Handle the get command."""
    import uuid as uuid_mod

    try:
        memory_id = uuid_mod.UUID(args.memory_id)
    except ValueError:
        print(f"Error: not a valid memory id: {args.memory_id}", file=sys.stderr)
        return 2

    mem = get_memory_by_id(memory_id)
    if mem is None:
        print("Memory not found.", file=sys.stderr)
        return 1

    # An explicit fetch-by-id is the access signal: it means someone actually
    # opened this memory, unlike appearing in a search result list.
    record_access([memory_id])

    if args.json:
        print(json.dumps(mem, indent=2, default=str))
    else:
        print(f"ID: {mem.get('id')}")
        print(f"Source: {mem.get('source')}")
        print(f"Created: {mem.get('created_at')}")
        print(f"Tags: {', '.join(mem.get('tags') or [])}")
        print(f"Importance: {mem.get('importance')}")
        print(f"Accessed: {mem.get('access_count')} times")
        print("-" * 40)
        print(mem.get("content"))
    return 0
