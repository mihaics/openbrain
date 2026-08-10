"""
Open Brain CLI.
Command-line interface for memory operations.
"""
import argparse
import sys


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog='openbrain',
        description='Open Brain - Memory management CLI'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Search command
    search_parser = subparsers.add_parser('search', help='Search memories')
    search_parser.add_argument('query', help='Search query')
    search_parser.add_argument('--limit', '-n', type=int, default=10, help='Max results')
    search_parser.add_argument('--source', '-s', help='Filter by source')
    search_parser.add_argument('--tag', '-t', help='Filter by tag')
    search_parser.add_argument('--json', action='store_true', help='Output as JSON')
    
    # Store command
    store_parser = subparsers.add_parser('store', help='Store a new memory')
    store_parser.add_argument('content', help='Content to store')
    store_parser.add_argument('--source', default='cli', help='Source (default: cli)')
    store_parser.add_argument('--tag', '-t', action='append', help='Add tag')
    store_parser.add_argument('--importance', '-i', type=float, default=0.5, help='Importance 0-1')
    
    # Stats command
    stats_parser = subparsers.add_parser('stats', help='Show statistics')
    stats_parser.add_argument('--json', action='store_true', help='Output as JSON')
    
    # Import command
    import_parser = subparsers.add_parser('import', help='Import from source')
    import_parser.add_argument('source', choices=['telegram', 'whatsapp', 'claude_code', 'gmail', 'file'], help='Source type')
    import_parser.add_argument('path', help='Path to import from')
    import_parser.add_argument('--limit', '-n', type=int, help='Limit number of imports')
    
    # Report command
    report_parser = subparsers.add_parser('report', help='Generate weekly report')
    report_parser.add_argument('--days', '-d', type=int, default=7, help='Number of days')
    report_parser.add_argument('--output', '-o', help='Output file')
    
    # Serve command
    serve_parser = subparsers.add_parser('serve', help='Start API server')
    serve_parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    serve_parser.add_argument('--port', '-p', type=int, default=8000, help='Port to bind to')
    serve_parser.add_argument('--reload', action='store_true', help='Auto-reload on changes')

    # Exec command
    exec_parser = subparsers.add_parser(
        'exec',
        help='Run a shell command directly or in the configured sandbox'
    )
    exec_parser.add_argument(
        '--sandbox',
        '-s',
        action='store_true',
        help='Run in OpenSandbox instead of directly on the host'
    )
    exec_parser.add_argument(
        '--timeout',
        '-t',
        type=int,
        default=60,
        help='Timeout in seconds'
    )
    exec_parser.add_argument(
        '--persist',
        '-p',
        action='store_true',
        help="Keep sandbox state after execution when supported"
    )
    exec_parser.add_argument(
        '--cwd',
        help='Working directory for execution'
    )
    exec_parser.add_argument(
        '--mount',
        help='Host path to mount into the sandbox when supported'
    )
    exec_parser.add_argument(
        '--allow-network',
        choices=['true', 'false'],
        help='Allow outbound network access in sandbox mode'
    )
    exec_parser.add_argument(
        'exec_command',
        nargs=argparse.REMAINDER,
        metavar='COMMAND',
        help='Command to execute. If omitted, stdin is used.'
    )

    # Reembed command
    reembed_parser = subparsers.add_parser(
        'reembed', help='Re-embed memories with NULL or zero embeddings'
    )
    reembed_parser.add_argument('--limit', '-n', type=int, default=100,
                                help='Max rows to examine (default: 100)')
    reembed_parser.add_argument('--dry-run', action='store_true',
                                help='Report what would change, write nothing')
    reembed_parser.add_argument('--skip-ids', default='',
                                help='Comma-separated memory ids to exclude. '
                                     'Three deterministically failing oldest '
                                     'rows would otherwise wedge every sweep, '
                                     'since selection is oldest-first.')

    backup_parser = subparsers.add_parser(
        'backup', help='Dump the database, verify it, and prune old dumps'
    )
    backup_parser.add_argument('--verify-only', action='store_true',
                               help='Re-verify the newest dump; write nothing')
    backup_parser.add_argument('--status', action='store_true',
                               help='Report backup state; mutate nothing')
    backup_parser.add_argument('--dry-run', action='store_true',
                               help='Print planned actions without executing')
    backup_parser.add_argument('--json', action='store_true',
                               help='Machine-readable output (--status only)')

    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    try:
        if args.command == 'search':
            from .search import search_memories_cmd
            return search_memories_cmd(args)
        elif args.command == 'store':
            from .store import store_memory_cmd
            return store_memory_cmd(args)
        elif args.command == 'stats':
            from .stats import stats_cmd
            return stats_cmd(args)
        elif args.command == 'import':
            from .import_data import import_cmd
            return import_cmd(args)
        elif args.command == 'report':
            from .report import report_cmd
            return report_cmd(args)
        elif args.command == 'serve':
            from .serve import serve_cmd
            return serve_cmd(args)
        elif args.command == 'exec':
            from .exec_command import exec_cmd
            return exec_cmd(args)
        elif args.command == 'reembed':
            from .reembed import reembed_cmd
            return reembed_cmd(args)
        elif args.command == 'backup':
            from .backup import backup_cmd
            return backup_cmd(args)
        else:
            parser.print_help()
            return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
