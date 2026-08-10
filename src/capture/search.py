"""Search Claude Code transcripts in place. No index, no table, no writes.

Transcripts are already durable files; copying 282 MB of raw conversation into
`memory` would drown 212 curated memories in their own search results, and an
index would cost storage, staleness and a backup-allowlist entry to save time
nobody can perceive - fully parsing 200 transcripts measures 0.6 s.
"""
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .extract import SECRET_PATTERNS, redact
from .transcript import decode_record

MAX_RECORD_BYTES = 1_000_000
DEFAULT_LIMIT = 20
DEFAULT_BUDGET_SECONDS = 10.0
SNIPPET_CHARS = 240
UUID_LEN = 36


def _looks_like_credential(query: str) -> bool:
    return any(p.search(query) for p in SECRET_PATTERNS)


def _entropy_redact(text: str) -> str:
    """Blank long unbroken high-entropy tokens the pattern list would miss.

    Pattern redaction covers seven known shapes; this is a coarse net for the
    rest. It is still incomplete, which the spec states rather than hides.
    """
    import math

    out = []
    for token in text.split(" "):
        core = token.strip(".,;:'\"()[]{}")
        if len(core) >= 32 and not any(c.isspace() for c in core):
            counts: Dict[str, int] = {}
            for ch in core:
                counts[ch] = counts.get(ch, 0) + 1
            entropy = -sum((c / len(core)) * math.log2(c / len(core))
                           for c in counts.values())
            if entropy > 4.0:
                out.append(token.replace(core, "[REDACTED]"))
                continue
        out.append(token)
    return " ".join(out)


def sanitize(text: str) -> str:
    """Redact the whole block, before matching and before clipping.

    Clipping first is unsound: a 240-character window can cut away the prefix
    a pattern needs to be recognised, leaving half a credential visible.
    """
    return _entropy_redact(redact(text))


@dataclass
class Match:
    persona: str
    project: str
    kind: str
    session_id: str
    agent_id: Optional[str]
    timestamp: Optional[str]
    role: str
    snippet: str

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class SearchResult:
    matches: List[Match] = field(default_factory=list)
    scanned_files: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    truncation_reasons: List[str] = field(default_factory=list)
    next_cursor: Optional[str] = None

    def note(self, reason: str) -> None:
        self.truncated = True
        if reason not in self.truncation_reasons:
            self.truncation_reasons.append(reason)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "matches": [m.as_dict() for m in self.matches],
            "scanned_files": self.scanned_files,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
            "truncation_reasons": self.truncation_reasons,
            "next_cursor": self.next_cursor,
        }


def _safe_open(root: Path, target: Path) -> Optional[int]:
    """Open a file inside `root` without following any symlink component.

    O_NOFOLLOW protects only the final component, so a project directory
    swapped for a symlink between resolution and open would still escape.
    Every component is opened descriptor-relatively instead.
    """
    try:
        rel = target.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None

    dir_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = rel.parts
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)

    try:
        if not os.path.stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _identity(path: Path, root: Path, persona: str) -> Dict[str, Any]:
    """Session vs subagent, from the file's position rather than its name."""
    if path.parent.name == "subagents":
        return {"persona": persona, "kind": "subagent",
                "session_id": path.parent.parent.name, "agent_id": path.stem}
    return {"persona": persona, "kind": "session",
            "session_id": path.stem, "agent_id": None}


def discover(roots: Dict[str, Path]) -> List[Tuple[str, Path, Path]]:
    """(persona, root, file) in lexicographic path order.

    Lexicographic, never mtime: mtime is mutable, so ordering by it lets an
    append move a file across a cursor boundary and duplicate or skip records.
    """
    seen = set()
    out = []
    for persona, root in roots.items():
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if not resolved.is_dir() or resolved in seen:
            continue
        seen.add(resolved)
        for path in sorted(resolved.rglob("*.jsonl")):
            out.append((persona, resolved, path))
    return sorted(out, key=lambda t: str(t[2]))


def search(query: str, roots: Dict[str, Path], *, limit: int = DEFAULT_LIMIT,
           project: Optional[str] = None, persona: Optional[str] = None,
           since: Optional[str] = None, include_subagents: bool = True,
           cursor: Optional[str] = None,
           budget_seconds: float = DEFAULT_BUDGET_SECONDS) -> SearchResult:
    """Case-insensitive substring search. No regex: see the spec."""
    result = SearchResult()
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    if _looks_like_credential(query):
        raise ValueError("refusing to search for a credential-shaped string")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    needle = query.casefold()
    started = time.monotonic()
    deadline = started + budget_seconds
    start_path, start_index = _parse_cursor(cursor, query, project, persona,
                                            since, include_subagents)

    for p_name, root, path in discover(roots):
        if persona and p_name != persona:
            continue
        if time.monotonic() > deadline:
            result.note("deadline")
            break
        if start_path and str(path) < start_path:
            continue

        ident = _identity(path, root, p_name)
        if ident["kind"] == "subagent" and not include_subagents:
            continue

        fd = _safe_open(root, path)
        if fd is None:
            result.note("unreadable_or_symlink")
            continue

        result.scanned_files += 1
        index = -1
        try:
            with os.fdopen(fd, "rb") as fh:
                for raw in fh:
                    index += 1
                    if str(path) == start_path and index <= start_index:
                        continue
                    if time.monotonic() > deadline:
                        result.note("deadline")
                        return _finish(result, started)
                    if len(raw) > MAX_RECORD_BYTES:
                        result.note("oversized_record")
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        result.note("malformed_record")
                        continue

                    decoded = decode_record(record)
                    if decoded is None:
                        continue
                    role, text = decoded

                    if project and record.get("cwd") != project:
                        continue
                    if since and (record.get("timestamp") or "") < since:
                        continue

                    # Redact the whole block, then match on the redacted text.
                    clean = sanitize(text)
                    pos = clean.casefold().find(needle)
                    if pos < 0:
                        continue

                    result.matches.append(Match(
                        project=record.get("cwd") or "", role=role,
                        timestamp=record.get("timestamp"),
                        snippet=_snippet(clean, pos), **ident))

                    if len(result.matches) >= limit:
                        result.note("match_limit")
                        result.next_cursor = _make_cursor(
                            path, index, query, project, persona, since,
                            include_subagents)
                        return _finish(result, started)
        except OSError:
            result.note("unreadable_or_symlink")
            continue

    return _finish(result, started)


def _snippet(text: str, pos: int) -> str:
    half = SNIPPET_CHARS // 2
    begin = max(0, pos - half)
    return ("…" if begin else "") + text[begin:begin + SNIPPET_CHARS].strip()


def _cursor_scope(query, project, persona, since, include_subagents) -> str:
    import hashlib
    raw = f"{query}|{project}|{persona}|{since}|{include_subagents}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _make_cursor(path, index, *scope) -> str:
    return f"{_cursor_scope(*scope)}:{index}:{path}"


def _parse_cursor(cursor, *scope) -> Tuple[Optional[str], int]:
    if not cursor:
        return None, -1
    try:
        digest, index, path = cursor.split(":", 2)
    except ValueError:
        raise ValueError("malformed cursor")
    if digest != _cursor_scope(*scope):
        # Reinterpreting a cursor against a different query would silently
        # page through the wrong search.
        raise ValueError("cursor does not match this query and filters")
    return path, int(index)


def _finish(result: SearchResult, start: Optional[float]) -> SearchResult:
    if start is not None:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
    return result
