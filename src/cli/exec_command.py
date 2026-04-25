"""
Exec command for the OpenBrain CLI.
"""
import asyncio
import sys
from argparse import Namespace

from ..sandbox import SandboxConfig, get_executor


def _read_command(args: Namespace) -> str:
    """Read the shell command from argv or stdin."""
    parts = args.exec_command or []
    command = ' '.join(parts).strip()
    if command:
        return command

    if not sys.stdin.isatty():
        command = sys.stdin.read().strip()
        if command:
            return command

    raise ValueError("No command provided. Pass a command or pipe one on stdin.")


async def _run(args: Namespace) -> int:
    command = _read_command(args)
    mode = 'sandbox' if args.sandbox else 'direct'
    config = None

    if args.sandbox and (args.mount or args.allow_network is not None):
        config = SandboxConfig()
        config.workspace_mount = args.mount
        if args.allow_network is not None:
            config.allow_egress = args.allow_network == 'true'

    executor = get_executor(mode=mode, config=config)

    if args.sandbox:
        result = await executor.run(
            command,
            timeout=args.timeout,
            working_dir=args.cwd,
            ephemeral=not args.persist,
        )
    else:
        result = await executor.run(
            command,
            timeout=args.timeout,
            working_dir=args.cwd,
        )

    if result.stdout:
        print(result.stdout, end='' if result.stdout.endswith('\n') else '\n')
    if result.stderr:
        print(result.stderr, file=sys.stderr, end='' if result.stderr.endswith('\n') else '\n')
    if result.error and not result.stderr:
        print(result.error, file=sys.stderr)

    if result.exit_code is None or result.exit_code < 0:
        return 1
    return result.exit_code


def exec_cmd(args: Namespace) -> int:
    """Handle the exec command."""
    try:
        return asyncio.run(_run(args))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
