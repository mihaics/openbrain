"""Durable progress: one global generation checkpoint, per-session boundaries.

The checkpoint is global, not per session. A per-session comparison breaks in
ordinary use: sessions A and B both sit at generation 1, A commits and the
database advances to 2, and B - still at 1, with the latest committed identity
belonging to A - would be refused forever. After the first committing session
every other session would be permanently blocked.
"""
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from .queue import atomic_write_json


class Interlock(Enum):
    OK = "ok"                 # checkpoint and database agree
    REBASE = "rebase"         # database ahead, explained by committed work
    REFUSE = "refuse"         # filesystem ahead: a restore happened
    UNINITIALIZED = "uninit"  # no checkpoint: refuse and require init


@dataclass
class Progress:
    root: Path

    # ---- paths -----------------------------------------------------------
    @property
    def checkpoint_path(self) -> Path:
        return self.root / "generation-checkpoint.json"

    @property
    def intent_path(self) -> Path:
        return self.root / "reconcile-intent.json"

    def boundary_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.progress"

    # ---- checkpoint ------------------------------------------------------
    def read_checkpoint(self) -> Optional[int]:
        try:
            return int(json.loads(self.checkpoint_path.read_text())["generation"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write_checkpoint(self, generation: int) -> None:
        atomic_write_json(self.checkpoint_path, {"generation": int(generation)})

    def classify(self, db_generation: int) -> Interlock:
        """Compare the global checkpoint against the database."""
        checkpoint = self.read_checkpoint()
        if checkpoint is None:
            # Never lazily initialized: restoring a pre-capture database and
            # re-running migrate looks exactly like a clean install while
            # stale progress may still sit on disk.
            return Interlock.UNINITIALIZED
        if checkpoint == db_generation:
            return Interlock.OK
        if checkpoint < db_generation:
            return Interlock.REBASE
        return Interlock.REFUSE

    # ---- per-session boundaries -----------------------------------------
    def read_boundary(self, session_id: str) -> Dict[str, Any]:
        try:
            return json.loads(self.boundary_path(session_id).read_text())
        except (OSError, ValueError):
            return {}

    def offset(self, session_id: str) -> int:
        return int(self.read_boundary(session_id).get("byte_offset", 0))

    def write_boundary(self, session_id: str, boundary: Dict[str, Any]) -> None:
        atomic_write_json(self.boundary_path(session_id), boundary)

    # ---- reconcile intent -----------------------------------------------
    def read_intent(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.intent_path.read_text())
        except (OSError, ValueError):
            return None

    def write_intent(self, payload: Dict[str, Any]) -> None:
        atomic_write_json(self.intent_path, payload)

    def clear_intent(self) -> None:
        self.intent_path.unlink(missing_ok=True)

    # ---- state inventory -------------------------------------------------
    def dirty_reasons(self) -> list:
        """Everything that must be absent for a clean initialization."""
        reasons = []
        if not self.root.exists():
            return reasons
        if list(self.root.glob("*.progress")):
            reasons.append("per-session progress exists")
        if list(self.root.glob("*.processing")):
            reasons.append("a claim is in progress")
        if list(self.root.glob("*.deadletter")):
            reasons.append("dead-lettered work exists")
        if list(self.root.glob("*.json")) and self.checkpoint_path.exists():
            pass
        if self.intent_path.exists():
            reasons.append("a reconcile intent exists")
        return reasons
