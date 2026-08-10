"""Reading Claude Code transcripts into conversation, and tracking boundaries.

Two measured facts drive this module. `Stop` fires per turn, not per session -
one real session carried 8 distinct promptIds - so progress is a boundary, not
a done-flag. And 75 of 85 `user` records in that session were `tool_result`
blocks, so filtering on the top-level `type` would feed tool output to the
model as if a human had written it.
"""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Record types that never contain conversation.
SKIP_RECORD_TYPES = {
    "file-history-snapshot", "file-history-delta", "mode", "permission-mode",
    "last-prompt", "attachment", "system", "summary",
}
# Content blocks that are machine traffic rather than something said.
SKIP_BLOCK_TYPES = {"tool_result", "tool_use", "thinking", "redacted_thinking"}


@dataclass
class Exchange:
    role: str
    text: str
    record_uuid: Optional[str] = None
    prompt_id: Optional[str] = None


@dataclass
class Boundary:
    """Where a session has been read up to. Ordering is by byte_offset."""
    session_id: str
    byte_offset: int
    last_record_uuid: Optional[str] = None
    last_prompt_id: Optional[str] = None

    @property
    def boundary_id(self) -> str:
        """Identity only - never used for ordering.

        Derived from the session and position, never from model output: a
        value derived from what the model said cannot identify the work that
        produced it.
        """
        raw = f"{self.session_id}|{self.byte_offset}|{self.last_record_uuid or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "byte_offset": self.byte_offset,
            "last_record_uuid": self.last_record_uuid,
            "last_prompt_id": self.last_prompt_id,
            "boundary_id": self.boundary_id,
        }


def _block_text(content: Any) -> str:
    """Pull human/assistant prose out of a message body."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in SKIP_BLOCK_TYPES:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def read_exchanges(
    path: Path,
    start_offset: int = 0,
) -> Tuple[List[Exchange], Boundary]:
    """Read conversation after `start_offset`, returning it and the new boundary.

    Offsets are byte offsets from a binary read: counting characters would
    corrupt the watermark on any multibyte UTF-8. A trailing partial line is
    left unread and its bytes are not counted, because the file is being
    appended to while we read it.
    """
    session_id = path.stem
    exchanges: List[Exchange] = []
    consumed = start_offset
    last_uuid = last_prompt = None

    with open(path, "rb") as f:
        f.seek(start_offset)
        data = f.read()

    for raw in data.split(b"\n"):
        line_len = len(raw) + 1
        if not raw.strip():
            consumed += line_len
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            # A torn final line: stop here and leave its bytes for next time.
            break
        consumed += line_len

        if record.get("type") in SKIP_RECORD_TYPES:
            continue
        if record.get("isMeta") or record.get("isSidechain"):
            continue
        if record.get("type") not in ("user", "assistant"):
            continue

        text = _block_text((record.get("message") or {}).get("content"))
        if not text:
            continue

        last_uuid = record.get("uuid") or last_uuid
        last_prompt = record.get("promptId") or last_prompt
        exchanges.append(Exchange(
            role=record["type"], text=text,
            record_uuid=record.get("uuid"), prompt_id=record.get("promptId"),
        ))

    # `consumed` can exceed the file when the last line had no newline; clamp.
    consumed = min(consumed, path.stat().st_size)
    return exchanges, Boundary(session_id, consumed, last_uuid, last_prompt)


def render(exchanges: List[Exchange], max_chars: int) -> str:
    """Render exchanges for the model, trimmed at exchange boundaries."""
    out, total = [], 0
    for ex in reversed(exchanges):
        piece = f"{ex.role}: {ex.text}"
        if total + len(piece) > max_chars and out:
            break
        out.append(piece)
        total += len(piece)
    return "\n\n".join(reversed(out))


def windows(exchanges: List[Exchange], max_chars: int, overlap: int = 2
            ) -> List[List[Exchange]]:
    """Split oversized conversation into overlapping windows.

    Overlapping rather than one trailing slice: a decision made early whose
    resolution lands late would otherwise be cut in half and lost from both
    pieces. Results are aggregated into a single boundary by the caller.
    """
    if not exchanges:
        return []
    if len(render(exchanges, max_chars * 10)) <= max_chars:
        return [exchanges]

    out, current, size = [], [], 0
    for ex in exchanges:
        piece = len(ex.text) + len(ex.role) + 2
        if size + piece > max_chars and current:
            out.append(current)
            current = current[-overlap:] if overlap else []
            size = sum(len(e.text) for e in current)
        current.append(ex)
        size += piece
    if current:
        out.append(current)
    return out
