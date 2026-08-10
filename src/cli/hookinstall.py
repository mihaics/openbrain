"""Install and remove the Stop hook in a Claude settings.json.

Editing a user's settings file is the riskiest side effect here, so it
preserves unknown keys and existing hooks, adds exactly one identifiable
entry, validates the result, and writes by atomic replacement.
"""
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

HOOK_COMMAND = str(Path(__file__).resolve().parents[2] / "bin" / "openbrain-capture-hook")
MARKER = "openbrain-capture-hook"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise RuntimeError(f"{path} is not valid JSON: {e}")


def _atomic_write(path: Path, data: dict) -> None:
    payload = json.dumps(data, indent=2) + "\n"
    json.loads(payload)  # never write something we cannot read back
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600

    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def install(path: Path) -> str:
    """Add one Stop hook entry, leaving everything else untouched."""
    data = _read(path)
    hooks = data.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])

    for group in stop:
        for hook in group.get("hooks", []):
            if MARKER in str(hook.get("command", "")):
                return "already installed"

    stop.append({
        "matcher": "*",
        "hooks": [{
            "type": "command",
            "command": HOOK_COMMAND,
            # Asynchronous: Claude waits for synchronous command hooks, and
            # this must never sit in the path of a turn.
            "async": True,
            "timeout": 5,
        }],
    })
    _atomic_write(path, data)
    return "installed"


def uninstall(path: Path) -> str:
    """Remove only our entry."""
    data = _read(path)
    stop = (data.get("hooks") or {}).get("Stop")
    if not stop:
        return "not installed"

    kept = []
    removed = False
    for group in stop:
        hooks = [h for h in group.get("hooks", [])
                 if MARKER not in str(h.get("command", ""))]
        if len(hooks) != len(group.get("hooks", [])):
            removed = True
        if hooks:
            kept.append({**group, "hooks": hooks})

    if kept:
        data["hooks"]["Stop"] = kept
    else:
        data["hooks"].pop("Stop", None)
        if not data["hooks"]:
            data.pop("hooks")

    _atomic_write(path, data)
    return "uninstalled" if removed else "not installed"


def install_cmd(args: Namespace) -> int:
    target = args.install_hook or args.uninstall_hook
    path = Path(target).expanduser()
    try:
        result = install(path) if args.install_hook else uninstall(path)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"{result}: {path}")
    return 0
