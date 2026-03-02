"""
Stats command for CLI.
"""
import json
from argparse import Namespace

from ..db.queries import get_memory_stats


def stats_cmd(args: Namespace) -> int:
    """Handle the stats command."""
    stats = get_memory_stats()
    
    if args.json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        print("=" * 40)
        print("Open Brain Statistics")
        print("=" * 40)
        
        print(f"\nTotal Memories: {stats.get('total', 0)}")
        
        # By source
        by_source = stats.get('by_source', {})
        if by_source:
            print("\nBy Source:")
            for source, count in by_source.items():
                print(f"  {source}: {count}")
        
        # Top tags
        top_tags = stats.get('top_tags', [])
        if top_tags:
            print("\nTop Tags:")
            for tag, count in top_tags[:10]:
                print(f"  #{tag}: {count}")
        
        # Weekly activity
        weekly = stats.get('weekly_activity', [])
        if weekly:
            print("\nWeekly Activity:")
            for day in weekly:
                print(f"  {day.get('date')}: {day.get('count')} memories")
        
        # Entities
        top_entities = stats.get('top_entities', [])
        if top_entities:
            print("\nTop Entities:")
            for entity in top_entities[:10]:
                print(f"  {entity.get('name')} ({entity.get('type')}): {entity.get('count')}")
    
    return 0
