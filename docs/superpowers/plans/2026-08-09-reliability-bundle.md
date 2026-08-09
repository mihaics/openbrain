# OpenBrain Reliability Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give OpenBrain a verified nightly backup, eliminate every code path that can write a corrupt or stale embedding, make the test suite runnable, and make the repository describe how the system actually runs.

**Architecture:** Four independent components. A new `openbrain backup` CLI subcommand produces snapshot-consistent `pg_dump -Fc` archives with sidecar manifests, verifies each archive's TOC and full payload, and prunes to the newest 60 — driven by a systemd --user timer. A new `create_validated_embedding()` helper plus an `UNSET` sentinel in `update_memory` closes the stale/corrupt embedding paths across all 13 call sites. Dev tooling and CI make the existing 46-test suite runnable. Documentation and stale operational scripts are reconciled with reality.

**Tech Stack:** Python 3.11+, PostgreSQL 18 + pgvector 0.8.1, psycopg2, argparse CLI, pytest, systemd --user, GitHub Actions.

**Source spec:** `docs/superpowers/specs/2026-08-09-reliability-bundle-design.md` (**revision 8**)

## Global Constraints

- Database: PostgreSQL 18 on `127.0.0.1:5433`, database/user `openbrain`. Role has `rolsuper=false`, `rolcreatedb=false`. **Never** assume otherwise.
- Credentials come from `src.db.connection.DatabaseConfig` (YAML defaults + per-field env overrides). Never read `config/settings.yaml` directly for DB settings.
- The database password is passed to child processes via the child's env dict only — **never** in argv, never via `os.environ` mutation.
- Embedding dimension always comes from the live column typmod via `get_vector_dim()`. **Never** hardcode 1024.
- No restore verification of any kind: no `CREATEDB`, no superuser step, no verification database. Verification is TOC allowlist + full payload decode, both privilege-free.
- Import style is mixed in this repo and must be respected per-module: `src/main.py` and `src/api/main.py` do `sys.path.insert(...)` then import top-level (`from db.queries import ...`); `src/cli/*` and `src/connectors/*` use relative imports (`from ..db.queries import ...`). Do not "fix" this as part of this plan. **New tests always import `src.<module>` and patch at the use site** — see "Import identity" above; the old top-level test style does not work for `src/cli/*`.
- `src/main.py` is an MCP **stdio** server. Never write to stdout from any code it imports — diagnostics go to `sys.stderr`.
- All timestamps written to disk are UTC.
- Backup files: directory `0700`, files `0600`.
- Sequence numbers are zero-padded to 6 digits in filenames; ordering logic parses
  the integer and never depends on the padding.
- New tests import `src.<module>` and patch at the use site. `sys.path` is touched in
  exactly one place, `tests/conftest.py`, and never in a test file.
- Commit after every task. No AI/Claude attribution in commit messages.

## Resolved decisions

**Snapshot machinery: keep it.** Earlier drafts left this conditional on a review that
timed out without answering. It is resolved: `snapshot_metadata` stays. It is ~12
lines, it was verified working against the live cluster (`SHOW transaction_isolation`
confirms `repeatable read` and `pg_export_snapshot()` succeeds — it would error under
READ COMMITTED), and it is what makes the runbook's manual row-count check exact.
**No task in this plan is conditional on anything.**

## Import identity — read this before writing any test

`src/cli/*` and `src/connectors/*` use relative imports (`from ..db.queries import`).
Importing them as top-level `cli.store` raises
`ImportError: attempted relative import beyond top-level package` — **verified**. They
must be imported as `src.cli.store`, `src.connectors.gmail`, and so on. `src` is a
real package (`pyproject.toml` includes `src*`), and the repo root is on the path
under an editable install, so no `sys.path` manipulation is needed in new tests.

`src/main.py` and `src/api/main.py` insert the `src` directory on `sys.path` and then
import top-level (`from db.queries import ...`), so a module can exist twice under two
identities (`db.queries` and `src.db.queries`) with independently patched state.

**Two rules follow from this, and every test in this plan obeys both:**

1. **Patch at the use site.** `patch('src.cli.store.create_validated_embedding')`,
   never `patch('src.embedder.create_validated_embedding')`.
2. **Take exception classes from the module under test.** Use
   `mcp.EmbeddingError` / `api.EmbeddingError`, never
   `from src.embedder import EmbeddingError`, when the code under test lives in
   `src/main.py` or `src/api/main.py`. Those modules do `from embedder import ...`,
   so their `EmbeddingError` is the **top-level** class; `src.embedder`'s is a
   different object (verified: `embedder is src.embedder` → `False`), and an
   `except EmbeddingError` clause would not catch it. Modules under `src/cli/` and
   `src/connectors/` use relative imports, so their identity already matches
   `src.embedder` — but sourcing the class from the module under test is correct
   everywhere, so do it unconditionally rather than reasoning per file.

Existing `tests/test_core.py` uses the older top-level style; leave it alone.

---

## File Structure

**Created:**

| Path | Responsibility |
| --- | --- |
| `src/cli/backup.py` | `openbrain backup`. **Created in Task 7** (header, imports, verification); Tasks 8 and 9 append the dump and the command surface |
| `src/cli/backup_store.py` | Filesystem primitives: paths, sequence state, atomic write, no-clobber publish, manifest read/write |
| `src/cli/reembed.py` | `openbrain reembed` — sweep NULL and zero embeddings |
| `deploy/systemd/openbrain-backup.service` | Tracked unit file |
| `deploy/systemd/openbrain-backup.timer` | Tracked timer file |
| `deploy/systemd/attic/openbrain-dashboard.service` | Preserved copy of the untracked orphan before deletion |
| `.github/workflows/test.yml` | CI: unit tests on push |
| `tests/test_backup_store.py` | Sequence state, publication, manifest, retention selection |
| `tests/test_backup.py` | **Created in Task 7** (imports + verification tests); Tasks 8 and 9 append dump and CLI-mode tests |
| `tests/test_embedding_integrity.py` | Validation helper, sentinel, per-call-site degradation |
| `tests/test_reembed.py` | Sweep ordering, optimistic guard, abort behavior |
| `tests/conftest.py` | Puts `src/` on `sys.path` so `import src.main` works |

**Modified:**

| Path | Change |
| --- | --- |
| `src/embedder/__init__.py` | Add `EmbeddingError`, `create_validated_embedding()`; fix `embed_batch` zeros → `None`; stderr logging |
| `src/db/queries.py` | `UNSET` sentinel + enforced content/embedding invariant; `_validate_embedding_dim` → `_validate_embedding` with zero rejection |
| `src/main.py` | Store, update, and search handlers use the validated helper; update clears stale embedding; docstring fix |
| `src/api/main.py` | Create, update (re-embed + re-tag + kwargs), and search paths |
| `src/cli/__init__.py` | Register `backup` and `reembed` subcommands |
| `src/cli/store.py`, `src/cli/search.py` | Use validated helper with correct degradation |
| `src/connectors/{gmail,file_watcher,claude_code,whatsapp,telegram}.py` | Use validated helper; store NULL on failure |
| `src/ingestion/importer.py` | Remove unused `create_embeddings` import |
| `pyproject.toml` | `[tool.pytest.ini_options]` |
| `README.md`, `.env.example`, `requirements.txt`, `SPEC.md` | Reconcile with reality |

**Deleted:** `docker-compose.yml`, `Dockerfile`, `web/Dockerfile`, `.dockerignore`, `scripts/backup.sh`, `scripts/healthcheck.sh`, `scripts/startup.sh`, `scripts/setup_db.py`, `~/.config/systemd/user/openbrain-dashboard.service`

**Task order rationale:** Task 1 first — nothing else can be tested until pytest runs in the venv. Tasks 2–5 (embedding integrity) before 6–11 (backup) because they are the higher-value fix and are independent. Task 12 last so documentation describes the finished state.

---

### Task 1: Dev tooling and CI

**Files:**
- Modify: `pyproject.toml`
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: nothing
- Produces: a working `.venv/bin/pytest`; the `integration` marker used by later tasks

- [ ] **Step 1: Install dev extras into the existing venv**

```bash
cd /home/mihai/tools/open-brain
.venv/bin/pip install -e ".[dev]"
```

- [ ] **Step 2: Confirm the suite passes**

Run: `.venv/bin/python -m pytest -q`
Expected: `46 passed`. If anything fails, stop and report — the baseline is known-green, so a failure means the install changed behavior and must be understood before proceeding.

- [ ] **Step 3: Add pytest configuration**

Append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "integration: requires a live PostgreSQL cluster; deselected by default",
]
addopts = "-m 'not integration'"
```

- [ ] **Step 4: Confirm configuration takes effect**

Run: `.venv/bin/python -m pytest -q`
Expected: `46 passed`, and no warning about unknown markers or unset `asyncio_mode`.

- [ ] **Step 5: Add `tests/conftest.py`**

`src/main.py` inserts the **repo root** on `sys.path` (its
`dirname(dirname(__file__))` resolves one level higher than `src/api/main.py`'s
does), then does `from db import connection`. That only resolves when it runs as a
script with `src/` as `sys.path[0]` — `import src.main` fails with
`ModuleNotFoundError: No module named 'db'`, **verified**. The MCP handler tests in
Task 4 need it importable, so `src/` goes on the path once, centrally:

```python
"""Shared test configuration.

src/main.py is written to run as a script: it inserts the repo root on
sys.path and then does `from db import connection`, which only resolves when
src/ is itself on the path. Putting src/ on the path here makes
`import src.main` work in tests without every test file repeating it.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, REPO_ROOT)
```

Create it at `tests/conftest.py`. This is the **only** place `sys.path` is touched;
individual test files must not.

- [ ] **Step 6: Add the CI workflow**

Create `.github/workflows/test.yml`:

```yaml
name: tests

on:
  push:
  pull_request:

jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pytest -q
```

No service containers: the suite is fully mocked and needs neither PostgreSQL nor Ollama.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .github/workflows/test.yml tests/conftest.py
git commit -m "test: make suite runnable in venv and add CI

pytest was declared in dev extras but never installed, so the 604-line
suite had never run here. Adds pytest config with an integration marker
deselected by default, and a workflow running unit tests on push."
```

---

### Task 2: Validated embedding helper

**Files:**
- Modify: `src/embedder/__init__.py`
- Create: `tests/test_embedding_integrity.py`

**Interfaces:**
- Consumes: `create_embedding(text) -> List[float]` (existing)
- Produces:
  - `class EmbeddingError(RuntimeError)`
  - `create_validated_embedding(text: str, expected_dim: Optional[int] = None) -> List[float]` — raises `EmbeddingError` on empty, all-zero, or dimension-mismatched output
  - `embed_batch(texts) -> List[Optional[List[float]]]`

**Design note:** `expected_dim` is a parameter rather than an internal `get_vector_dim()` call. The embedder must not import the DB layer — the repo's mixed import styles mean a cross-package import would break either the CLI or the MCP/API path, and an embedder knowing about Postgres is the wrong boundary anyway. DB-connected callers pass `get_vector_dim()` using whatever import idiom already works in their module.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embedding_integrity.py`:

```python
"""Tests for embedding validation and stale-embedding prevention."""
import os
import sys
from unittest.mock import Mock, patch

import pytest


class TestCreateValidatedEmbedding:
    """create_validated_embedding rejects unusable vectors."""

    def test_returns_valid_embedding(self):
        from src.embedder import create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.1] * 1024):
            result = create_validated_embedding("text", expected_dim=1024)

        assert result == [0.1] * 1024

    def test_rejects_all_zero_vector(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.0] * 1024):
            with pytest.raises(EmbeddingError, match="all-zero"):
                create_validated_embedding("text", expected_dim=1024)

    def test_rejects_empty_embedding(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[]):
            with pytest.raises(EmbeddingError, match="empty"):
                create_validated_embedding("text", expected_dim=1024)

    def test_rejects_dimension_mismatch(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.1] * 768):
            with pytest.raises(EmbeddingError, match="dimension"):
                create_validated_embedding("text", expected_dim=1024)

    def test_skips_dimension_check_when_unknown(self):
        from src.embedder import create_validated_embedding

        with patch('src.embedder.create_embedding', return_value=[0.1] * 768):
            result = create_validated_embedding("text", expected_dim=None)

        assert len(result) == 768

    def test_propagates_provider_failure_as_embedding_error(self):
        from src.embedder import EmbeddingError, create_validated_embedding

        with patch('src.embedder.create_embedding', side_effect=RuntimeError("ollama down")):
            with pytest.raises(EmbeddingError, match="ollama down"):
                create_validated_embedding("text", expected_dim=1024)


class TestEmbedBatchNullPropagation:
    """embed_batch yields None for failed items, never a zero vector."""

    def test_failed_item_becomes_none(self):
        from src.embedder import EmbedderConfig, OllamaEmbedder

        config = Mock(spec=EmbedderConfig)
        config.dimensions = 1024
        config.model = "qwen3-embedding:0.6b"
        config.ollama_base_url = "http://localhost:11434"

        embedder = OllamaEmbedder(config)

        with patch.object(embedder, 'embed', side_effect=[[0.1] * 1024, RuntimeError("boom")]):
            result = embedder.embed_batch(["ok", "fails"])

        assert result[0] == [0.1] * 1024
        assert result[1] is None, "failed item must be None, never a zero vector"

    def test_never_emits_zero_vector(self):
        from src.embedder import EmbedderConfig, OllamaEmbedder

        config = Mock(spec=EmbedderConfig)
        config.dimensions = 1024
        config.model = "qwen3-embedding:0.6b"
        config.ollama_base_url = "http://localhost:11434"

        embedder = OllamaEmbedder(config)

        with patch.object(embedder, 'embed', side_effect=RuntimeError("boom")):
            result = embedder.embed_batch(["fails"])

        assert result == [None]
        assert [0.0] * 1024 not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_embedding_integrity.py -v`
Expected: FAIL — `ImportError: cannot import name 'EmbeddingError' from 'embedder'`, and the batch tests failing on the zero-vector assertion.

- [ ] **Step 3: Add `EmbeddingError` and `create_validated_embedding`**

In `src/embedder/__init__.py`, after the existing `create_embeddings` convenience function:

```python
class EmbeddingError(RuntimeError):
    """Raised when an embedding cannot be produced or is unusable.

    Callers treat this as "no embedding available" and degrade — storing a
    NULL embedding, or falling back to text-only search. It must never
    propagate to a user as a failed store: losing a memory is worse than
    storing one that is temporarily absent from vector search.
    """


def create_validated_embedding(
    text: str,
    expected_dim: Optional[int] = None,
) -> List[float]:
    """Create an embedding, rejecting output that would corrupt the database.

    Validation happens here, in the caller's try block, rather than in the
    database layer. The DB-layer check in queries._validate_embedding runs
    after the caller's exception handling has already completed, so relying
    on it alone turns a degraded embedding into a lost memory.

    Args:
        text: Content to embed.
        expected_dim: Declared dimension of the target column, normally
            get_vector_dim(). None skips the dimension check.

    Raises:
        EmbeddingError: provider failed, or returned an empty, all-zero, or
            wrongly-dimensioned vector.
    """
    try:
        embedding = create_embedding(text)
    except Exception as e:
        raise EmbeddingError(str(e)) from e

    if not embedding:
        raise EmbeddingError("embedder returned an empty embedding")

    if all(v == 0.0 for v in embedding):
        raise EmbeddingError(
            "embedder returned an all-zero embedding, which is not a valid "
            "vector for cosine similarity"
        )

    if expected_dim and len(embedding) != expected_dim:
        raise EmbeddingError(
            f"embedding dimension mismatch: got {len(embedding)}, "
            f"target column is vector({expected_dim})"
        )

    return embedding
```

- [ ] **Step 4: Fix `embed_batch` in `OllamaEmbedder`**

Replace the body at `src/embedder/__init__.py:330-341`:

```python
    def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed several texts, yielding None for any that fail.

        A failed item is None, never a zero vector. A zero vector is not a
        missing embedding: pgvector's cosine distance against it is NaN, so
        the row participates in ranking and duplicate detection with an
        undefined score instead of being correctly excluded.
        """
        embeddings: List[Optional[List[float]]] = []

        for text in texts:
            try:
                embeddings.append(self.embed(text))
            except Exception as e:
                # stderr, not stdout: src/main.py is an MCP stdio server and
                # anything on stdout corrupts the protocol stream.
                print(f"Error embedding text: {e}", file=sys.stderr)
                embeddings.append(None)

        return embeddings
```

Add `import sys` to the module imports if absent, and `Optional` to the `typing` import.

- [ ] **Step 5: Update the `create_embeddings` signature**

```python
def create_embeddings(texts: List[str]) -> List[Optional[List[float]]]:
    """Convenience function to create multiple embeddings.

    Failed items are None. Callers must handle that before inserting.
    """
    return get_embedder().embed_batch(texts)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_embedding_integrity.py -v`
Expected: all 8 PASS.

- [ ] **Step 7: Confirm no regression**

Run: `.venv/bin/python -m pytest -q`
Expected: all green, no failures (46 baseline plus the new tests).

- [ ] **Step 8: Commit**

```bash
git add src/embedder/__init__.py tests/test_embedding_integrity.py
git commit -m "feat(embedder): add validated embedding helper, stop emitting zero vectors

embed_batch substituted [0.0] * dim on failure. A zero vector is not a
missing embedding - pgvector's cosine distance against it is NaN, so the
row ranks with an undefined score rather than being excluded. It now
yields None.

Adds create_validated_embedding, which rejects empty, all-zero, and
wrongly-dimensioned output inside the caller's try block. The existing
DB-layer check runs after caller exception handling, so on its own it
converts a degraded embedding into a lost memory."
```

---

### Task 3: `update_memory` sentinel and enforced invariant

**Files:**
- Modify: `src/db/queries.py`
- Modify: `tests/test_embedding_integrity.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `UNSET` module-level sentinel in `src/db/queries.py`
  - `update_memory(memory_id, content=None, tags=None, source=None, importance=None, embedding=UNSET, entities=None, tag_sources=None) -> bool` — `embedding=None` now means `SET embedding = NULL`; supplying `content` while leaving `embedding` as `UNSET` raises `ValueError`
  - `_validate_embedding(embedding) -> Optional[List[float]]` (renamed from `_validate_embedding_dim`, now also rejects all-zero vectors)

**Why raise rather than auto-NULL:** auto-NULLing would silently discard a valid embedding a caller meant to supply, and hide the mistake. Only the two handlers update content, and both are changed in Task 4 to pass an embedding explicitly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_embedding_integrity.py`:

```python
class TestUpdateMemorySentinel:
    """update_memory distinguishes 'leave alone' from 'clear'."""

    def _mock_cursor(self, mock_get_cursor):
        ctx = Mock()
        ctx.__enter__ = Mock(return_value=ctx)
        ctx.__exit__ = Mock(return_value=False)
        ctx.rowcount = 1
        mock_get_cursor.return_value = ctx
        return ctx

    @patch('src.db.queries.get_db_cursor')
    def test_none_embedding_sets_null(self, mock_get_cursor):
        from src.db import queries

        ctx = self._mock_cursor(mock_get_cursor)
        queries.update_memory(uuid.uuid4(), content="new", embedding=None)

        sql = ctx.execute.call_args[0][0]
        assert "embedding = NULL" in sql

    @patch('src.db.queries.get_db_cursor')
    def test_unset_embedding_omits_column(self, mock_get_cursor):
        from src.db import queries

        ctx = self._mock_cursor(mock_get_cursor)
        queries.update_memory(uuid.uuid4(), tags=["a"])

        sql = ctx.execute.call_args[0][0]
        assert "embedding" not in sql

    @patch('src.db.queries.get_db_cursor')
    def test_content_without_embedding_decision_raises(self, mock_get_cursor):
        from src.db import queries

        self._mock_cursor(mock_get_cursor)
        with pytest.raises(ValueError, match="embedding"):
            queries.update_memory(uuid.uuid4(), content="new")

    @patch('src.db.queries.get_db_cursor')
    def test_tags_only_update_allowed(self, mock_get_cursor):
        from src.db import queries

        ctx = self._mock_cursor(mock_get_cursor)
        assert queries.update_memory(uuid.uuid4(), tags=["a"], importance=0.7) is True


class TestValidateEmbedding:
    """Database-layer backstop rejects zero vectors."""

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_rejects_all_zero(self, _mock_dim):
        from src.db import queries

        with pytest.raises(ValueError, match="zero"):
            queries._validate_embedding([0.0] * 1024)

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_rejects_wrong_dimension(self, _mock_dim):
        from src.db import queries

        with pytest.raises(ValueError, match="dimension"):
            queries._validate_embedding([0.1] * 768)

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_accepts_valid(self, _mock_dim):
        from src.db import queries

        assert queries._validate_embedding([0.1] * 1024) == [0.1] * 1024

    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_passes_none_through(self, _mock_dim):
        from src.db import queries

        assert queries._validate_embedding(None) is None

    @patch('src.db.queries.get_db_cursor')
    @patch('src.db.queries.get_vector_dim', return_value=1024)
    def test_search_path_still_validates(self, _mock_dim, mock_get_cursor):
        """search_memories used _validate_embedding_dim at line 170.

        The rename must not leave a NameError on the vector-search path, which
        no existing test covers because they all pass embedding=None.
        """
        from src.db import queries

        ctx = Mock()
        ctx.__enter__ = Mock(return_value=ctx)
        ctx.__exit__ = Mock(return_value=False)
        ctx.fetchall.return_value = []
        mock_get_cursor.return_value = ctx

        results = queries.search_memories("q", embedding=[0.1] * 1024, limit=5)
        assert results == []
```

Add `import uuid` to the test file's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_embedding_integrity.py -k "Sentinel or ValidateEmbedding" -v`
Expected: FAIL — `_validate_embedding` does not exist; content-without-embedding does not raise.

- [ ] **Step 3: Rename and extend the validator**

In `src/db/queries.py`, replace `_validate_embedding_dim`:

```python
UNSET = object()
"""Sentinel distinguishing "field not supplied" from an explicit None.

update_memory previously treated None as "do not update this field", which
made it impossible to clear an embedding. Callers that must clear one now
pass embedding=None; callers leaving it alone pass nothing.
"""


def _validate_embedding(embedding: Optional[List[float]]) -> Optional[List[float]]:
    """Reject embeddings that would corrupt vector search. None passes through.

    This is a backstop. Callers should validate via
    embedder.create_validated_embedding inside their own try block, because
    this function raises after their exception handling has finished.
    """
    if embedding is None:
        return None

    if all(v == 0.0 for v in embedding):
        raise ValueError(
            "Refusing to store an all-zero embedding: cosine distance "
            "against it is NaN, which corrupts ranking and duplicate "
            "detection. Store NULL instead."
        )

    expected = get_vector_dim()
    if expected and len(embedding) != expected:
        raise ValueError(
            f"Embedding dimension mismatch: got {len(embedding)}, "
            f"memory.embedding column is vector({expected}). "
            f"Reconfigure the embedder provider/model or run an ALTER TABLE."
        )
    return embedding
```

**Update all three call sites.** `grep -n "_validate_embedding_dim" src/db/queries.py`
returns line 84 (`insert_memory`), **line 170 (`search_memories`)**, and line 380
(`update_memory`). Missing line 170 makes every vector search raise `NameError`, and
no existing test catches it because they all pass `embedding=None`. After editing, the
grep must return only the definition.

- [ ] **Step 4: Add the sentinel and invariant to `update_memory`**

Change the signature and the embedding branch:

```python
def update_memory(
    memory_id: uuid.UUID,
    content: Optional[str] = None,
    tags: Optional[List[str]] = None,
    source: Optional[str] = None,
    importance: Optional[float] = None,
    embedding: Any = UNSET,
    entities: Optional[Dict] = None,
    tag_sources: Optional[Dict] = None,
) -> bool:
    """Partially update a memory. Only supplied fields are written.

    embedding is three-valued: UNSET leaves the column alone, None sets it
    to NULL, a list replaces it.

    Raises:
        ValueError: content supplied without an embedding decision. Writing
            new content over an old vector leaves the row semantically
            mismatched, findable by a query that matches text it no longer
            contains.
    """
    if content is not None and embedding is UNSET:
        raise ValueError(
            "update_memory: content was supplied without an embedding "
            "decision. Pass a fresh embedding, or embedding=None to clear "
            "the stale one. Leaving the old vector attached to new content "
            "silently corrupts vector search."
        )

    sets: List[str] = []
    params: List[Any] = []
    ...
    if embedding is not UNSET:
        if embedding is None:
            sets.append("embedding = NULL")
        else:
            sets.append("embedding = %s")
            params.append(_validate_embedding(embedding))
```

Keep the remaining field branches unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_embedding_integrity.py -v`
Expected: all PASS (8 from Task 2 + 8 new).

- [ ] **Step 6: Confirm no regression**

Run: `.venv/bin/python -m pytest -q`
Expected: all green. If an existing test calls `_validate_embedding_dim` by name, update that test — the rename is intentional.

- [ ] **Step 7: Commit**

```bash
git add src/db/queries.py tests/test_embedding_integrity.py
git commit -m "fix(db): allow clearing an embedding, refuse content updates that orphan one

update_memory treated None as 'do not update', so a failed re-embed left
the old vector attached to new content - a row findable by a query
matching text it no longer contains. An UNSET sentinel now separates
'leave alone' from 'clear', and supplying content without an embedding
decision raises rather than silently keeping the stale vector.

_validate_embedding_dim becomes _validate_embedding and also rejects
all-zero vectors, which previously passed the length-only check."
```

---

### Task 4: Wire all 13 embedding call sites

**Files:**
- Modify: `src/main.py` (store `~756`, update `~824`, search `~718`), `src/api/main.py` (create `162`, update `241`, search `296`), `src/cli/store.py:30`, `src/cli/search.py:20`, `src/connectors/gmail.py:209`, `src/connectors/file_watcher.py:140`, `src/connectors/claude_code.py:138`, `src/connectors/whatsapp.py:112`, `src/connectors/telegram.py:108`, `src/ingestion/importer.py:12`
- Modify: `tests/test_embedding_integrity.py`

**Interfaces:**
- Consumes: `create_validated_embedding`, `EmbeddingError` (Task 2); `UNSET` (Task 3)
- Produces: no new API. Every site degrades correctly.

**Degradation contract:**

| Site kind | On `EmbeddingError` |
| --- | --- |
| Store / create / connector import | Store the row with a NULL embedding |
| Update | Pass `embedding=None` to clear the stale vector |
| Search | Leave the query embedding `None` → text-only search. Never raise |

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_embedding_integrity.py`:

```python
class TestCallSiteDegradation:
    """Every embedding call site degrades instead of losing data."""

    def test_cli_store_stores_null_on_embedding_error(self):
        from src.cli.store import store_memory_cmd
        from src.embedder import EmbeddingError

        args = Mock()
        args.content = "content"
        args.source = "cli"
        args.tag = []
        args.importance = 0.5

        with patch('src.cli.store.create_validated_embedding',
                   side_effect=EmbeddingError("down")), \
             patch('src.cli.store.insert_memory', return_value=uuid.uuid4()) as mock_insert, \
             patch('src.cli.store.get_vector_dim', return_value=1024):
            rc = store_memory_cmd(args)

        assert rc == 0
        assert mock_insert.call_args.kwargs['embedding'] is None

    def test_cli_search_degrades_to_text_only(self):
        from src.cli.search import search_memories_cmd
        from src.embedder import EmbeddingError

        args = Mock()
        args.query = "q"
        args.limit = 5
        args.source = None
        args.tag = None
        args.json = True

        with patch('src.cli.search.create_validated_embedding',
                   side_effect=EmbeddingError("down")), \
             patch('src.cli.search.search_memories', return_value=[]) as mock_search, \
             patch('src.cli.search.get_vector_dim', return_value=1024):
            rc = search_memories_cmd(args)

        assert rc == 0
        assert mock_search.call_args.kwargs['embedding'] is None

    @pytest.mark.parametrize("module,text_field", [
        ("src.connectors.gmail", "content"),
        ("src.connectors.file_watcher", "body"),
        ("src.connectors.claude_code", "text"),
        ("src.connectors.whatsapp", "text"),
        ("src.connectors.telegram", "text"),
    ])
    def test_connectors_import_validated_helper(self, module, text_field):
        """Every connector must route through the validated helper.

        A grep-style assertion rather than a behavioral one: the connectors
        need live inputs to exercise, but the thing that must not regress is
        that none of them calls the raw embedder.
        """
        import importlib
        mod = importlib.import_module(module)
        assert hasattr(mod, 'create_validated_embedding'), (
            f"{module} must import create_validated_embedding"
        )
        assert not hasattr(mod, 'create_embedding'), (
            f"{module} still imports the unvalidated create_embedding"
        )


class TestMcpDegradation:
    """MCP handlers degrade rather than failing or keeping stale vectors."""

    async def test_store_stores_null_on_zero_vector(self):
        """A zero vector from the provider must yield a stored memory with a
        NULL embedding and embedded: false - never an exception."""
        import json as jsonlib
        import src.main as mcp

        # src.main does `from embedder import ...`, so its EmbeddingError is
        # the top-level `embedder` class - NOT src.embedder's. They are
        # distinct classes once both identities load (verified), so raising
        # src.embedder.EmbeddingError would sail past main's except clause.
        # Always take the class from the module under test.
        with patch('src.main.create_validated_embedding',
                   side_effect=mcp.EmbeddingError("all-zero embedding")), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.insert_memory', return_value=uuid.uuid4()) as mock_insert, \
             patch('src.main.extract_entities', return_value={}), \
             patch('src.main.auto_tag', return_value={"auto": "auto"}):
            result = await mcp.handle_memory_store({
                "content": "text", "source": "test",
            })

        payload = jsonlib.loads(result[0].text)
        assert payload["embedded"] is False
        assert mock_insert.call_args.kwargs['embedding'] is None

    async def test_update_clears_stale_embedding_on_failure(self):
        """The original bug: a failed re-embed left the old vector attached to
        new content."""
        import src.main as mcp

        with patch('src.main.create_validated_embedding',
                   side_effect=mcp.EmbeddingError("down")), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.update_memory', return_value=True) as mock_update, \
             patch('src.main.get_memory_by_id', return_value={"source": "test"}), \
             patch('src.main.extract_entities', return_value={}), \
             patch('src.main.auto_tag', return_value={"auto": "auto"}):
            await mcp.handle_memory_update({
                "memory_id": str(uuid.uuid4()), "content": "new content",
            })

        assert mock_update.call_args.kwargs['embedding'] is None, (
            "a failed re-embed must clear the stale vector, not keep it"
        )

    async def test_search_degrades_to_text_only(self):
        import src.main as mcp

        with patch('src.main.create_validated_embedding',
                   side_effect=mcp.EmbeddingError("down")), \
             patch('src.main.get_vector_dim', return_value=1024), \
             patch('src.main.search_memories', return_value=[]) as mock_search:
            await mcp.handle_memory_search({"query": "q"})

        assert mock_search.call_args.kwargs['embedding'] is None


class TestRestDegradation:
    """The REST update endpoint never re-embedded at all."""

    async def test_update_reembeds_and_retags(self):
        from src.api.main import MemoryUpdate, update_memory_endpoint

        mid = str(uuid.uuid4())
        with patch('src.api.main.create_validated_embedding',
                   return_value=[0.1] * 1024), \
             patch('src.api.main.get_vector_dim', return_value=1024), \
             patch('src.api.main.extract_entities', return_value={"urls": []}), \
             patch('src.api.main.auto_tag', return_value={"auto": "auto"}), \
             patch('src.api.main.update_memory', return_value=True) as mock_update:
            await update_memory_endpoint(mid, MemoryUpdate(
                content="new", tags=["a"], source="web", importance=0.5,
            ))

        kwargs = mock_update.call_args.kwargs
        assert kwargs['embedding'] == [0.1] * 1024, "REST update must re-embed"
        assert kwargs['entities'] == {"urls": []}, "and re-extract entities"
        assert "auto" in kwargs['tags'], "and re-tag"

    async def test_update_clears_embedding_on_failure(self):
        import src.api.main as api
        from src.api.main import MemoryUpdate, update_memory_endpoint

        # Same identity trap as the MCP tests: api/main.py imports embedder
        # top-level, so take the exception class from the module under test.
        mid = str(uuid.uuid4())
        with patch('src.api.main.create_validated_embedding',
                   side_effect=api.EmbeddingError("down")), \
             patch('src.api.main.get_vector_dim', return_value=1024), \
             patch('src.api.main.extract_entities', return_value={}), \
             patch('src.api.main.auto_tag', return_value={"auto": "auto"}), \
             patch('src.api.main.update_memory', return_value=True) as mock_update:
            await update_memory_endpoint(mid, MemoryUpdate(
                content="new", tags=["a"], source="web", importance=0.5,
            ))

        assert mock_update.call_args.kwargs['embedding'] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_embedding_integrity.py -k CallSite -v`
Expected: FAIL — `cli.store` has no attribute `create_validated_embedding`.

- [ ] **Step 3: Update `src/cli/store.py`**

```python
from ..db.connection import get_vector_dim
from ..db.queries import insert_memory
from ..embedder import EmbeddingError, create_validated_embedding
```

Replace the embedding block:

```python
    embedding = None
    try:
        embedding = create_validated_embedding(content, get_vector_dim())
    except EmbeddingError as e:
        print(f"Warning: storing without embedding: {e}", file=sys.stderr)
```

- [ ] **Step 4: Update `src/cli/search.py`**

```python
from ..db.connection import get_vector_dim
from ..db.queries import search_memories
from ..embedder import EmbeddingError, create_validated_embedding
```

```python
    embedding = None
    try:
        embedding = create_validated_embedding(query, get_vector_dim())
    except EmbeddingError as e:
        print(f"Warning: text-only search, no query embedding: {e}", file=sys.stderr)
```

Ensure `search_memories` is called with `embedding=embedding` as a keyword argument.

- [ ] **Step 5: Update the five connectors**

In each of `gmail.py`, `file_watcher.py`, `claude_code.py`, `whatsapp.py`, `telegram.py`, replace the import:

```python
from ..db.connection import get_vector_dim
from ..embedder import EmbeddingError, create_validated_embedding
```

and the call (variable name differs per file — `content`, `body`, `text`):

```python
            embedding = None
            try:
                embedding = create_validated_embedding(text, get_vector_dim())
            except EmbeddingError as e:
                print(f"Warning: importing without embedding: {e}", file=sys.stderr)
```

Add `import sys` where absent. Keep each file's existing variable name.

- [ ] **Step 6: Update `src/api/main.py`**

Imports (top-level style — `src` is on `sys.path` in this module):

```python
from db.connection import get_vector_dim, init_db
from db.queries import UNSET  # only if referenced directly
from src.embedder import EmbeddingError, create_validated_embedding
```

Create path (`~162`):

```python
    embedding = None
    try:
        embedding = create_validated_embedding(memory.content, get_vector_dim())
    except EmbeddingError as e:
        print(f"Warning: storing without embedding: {e}", file=sys.stderr)
```

Search path (`~296`): same pattern, leaving `embedding = None` on failure.

Update endpoint — replace the positional call entirely. It currently never re-embeds and never re-extracts entities or tags, so every web-UI edit orphaned its embedding:

```python
@app.put("/memories/{memory_id}")
async def update_memory_endpoint(memory_id: str, update: MemoryUpdate):
    """Update a memory, re-embedding and re-tagging its new content."""
    import uuid as uuid_mod
    try:
        mid = uuid_mod.UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")

    entities = extract_entities(update.content)
    tag_map = auto_tag(update.content, entities, update.source, update.tags)

    embedding = None
    try:
        embedding = create_validated_embedding(update.content, get_vector_dim())
    except EmbeddingError as e:
        print(f"Warning: clearing embedding on update: {e}", file=sys.stderr)

    updated = update_memory(
        mid,
        content=update.content,
        tags=list(tag_map.keys()),
        source=update.source,
        importance=update.importance,
        embedding=embedding,
        entities=entities,
        tag_sources=tag_map,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "updated", "id": memory_id}
```

Add `from extractors.entities import extract_entities` and `from extractors.tagger import auto_tag` if absent. Note `embedding=embedding` passes `None` on failure, which now means "clear it" — exactly the intent.

- [ ] **Step 7: Update `src/main.py`**

Import: `from embedder import EmbedderConfig, EmbeddingError, create_validated_embedding`

Store handler (`~756`):

```python
    embedding = None
    embedded = False
    try:
        embedding = await asyncio.to_thread(
            create_validated_embedding, content, get_vector_dim()
        )
        embedded = True
    except EmbeddingError as e:
        print(f"Warning: storing without embedding: {e}", file=sys.stderr)
```

Update handler (`~824`) — the stale-embedding fix. Always set the key so `update_memory`'s invariant is satisfied and a failure clears the old vector:

```python
        try:
            update_kwargs["embedding"] = await asyncio.to_thread(
                create_validated_embedding, content, get_vector_dim()
            )
            reembedded = True
        except EmbeddingError as e:
            print(f"Warning: re-embed failed, clearing embedding: {e}", file=sys.stderr)
            update_kwargs["embedding"] = None
```

Search handler (`~718`):

```python
    embedding = None
    if query.strip():
        try:
            embedding = await asyncio.to_thread(
                create_validated_embedding, query, get_vector_dim()
            )
        except EmbeddingError as e:
            print(f"Warning: text-only search: {e}", file=sys.stderr)
```

- [ ] **Step 8: Remove the dead import**

In `src/ingestion/importer.py`, delete line 12 (`from ..embedder import create_embeddings`). It has never been called — every import path inserts `embedding=None` (lines 69, 114, 157) under a comment claiming batching. Batching is out of scope; leave those inserts alone.

- [ ] **Step 9: Run tests**

Run: `.venv/bin/python -m pytest tests/test_embedding_integrity.py -v`
Expected: all PASS, including the five parametrized connector checks, the three MCP
degradation tests, and the two REST update tests. If an MCP test fails on import,
check that the patch target is `src.main.<name>` — patching `src.embedder.<name>`
will not affect the name already bound into `src.main`'s namespace.

- [ ] **Step 10: Verify no raw call sites remain**

Run:
```bash
grep -rn "create_embedding(" src/ --include='*.py' \
  | grep -v "create_validated_embedding" \
  | grep -v "^src/embedder/__init__.py:"
```
Expected: **no output.** The two allowed occurrences both live in
`src/embedder/__init__.py` — the `def create_embedding` definition and the
`create_embedding(text)` call inside `create_validated_embedding` — so that file is
excluded wholesale rather than by pattern. Filtering only on `def create_embedding`
would flag the helper's own internal call and report a false violation.

- [ ] **Step 11: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green. Exact counts are not asserted here — earlier tasks add tests, so
a hardcoded total would be wrong by the time this runs. What matters: zero failures,
and no test skipped other than `integration`-marked ones.

- [ ] **Step 12: Commit**

```bash
git add src/main.py src/api/main.py src/cli/store.py src/cli/search.py \
        src/connectors/ src/ingestion/importer.py tests/test_embedding_integrity.py
git commit -m "fix: validate embeddings at every call site, re-embed on REST update

Thirteen call sites called the embedder directly, so a bad provider
response reached the DB-layer check after each caller's exception
handling had finished - turning a degraded embedding into a lost memory
or a failed search.

PUT /memories/{id} was worse: it passed four positional arguments and
never re-embedded at all, so every edit through the web UI left the old
vector attached to new content. It now re-embeds, re-extracts entities
and tags, and uses keyword arguments.

Stores and connectors degrade to a NULL embedding; the three search
paths degrade to text-only. Also drops an unused create_embeddings
import from the importer."
```

---

### Task 5: `openbrain reembed`

**Files:**
- Create: `src/cli/reembed.py`, `tests/test_reembed.py`
- Modify: `src/cli/__init__.py`

**Interfaces:**
- Consumes: `create_validated_embedding`, `EmbeddingError` (Task 2); `get_vector_dim`, `get_db_cursor`
- Produces: `reembed_cmd(args: Namespace) -> int`; `run_sweep(limit: int, dry_run: bool, skip_ids: Optional[List[str]] = None) -> ReembedReport`; `_parse_skip_ids(raw: str) -> List[str]`; `acquire_lock()` context manager; `ReembedReport` dataclass with fields `selected`, `attempted`, `reembedded`, `zeros_repaired`, `skipped_changed`, `failed`, `unprocessed`, `aborted`, `dry_run` and a `check_invariant()` method

**Contract (from the spec, all of it load-bearing):**
- `--limit N` bounds **rows examined**, covering zero-repair and NULL sweep together.
- Order `created_at ASC, id ASC` — `id` is the stable tie-breaker.
- Each row commits individually; a later failure never rolls back earlier successes.
- Zero vectors are **NULLed first**, before any replacement attempt. Waiting for a successful replacement would leave poisoned rows poisoned whenever the embedder is down — and since the command aborts after 3 consecutive failures, a sweep starting with Ollama down would otherwise repair nothing.
- Optimistic guard: `WHERE id = %s AND content = %s`. Zero rows affected means the row changed underneath; report skipped rather than attaching a vector computed from stale content.
- Abort after 3 consecutive `EmbeddingError`s, exit non-zero.
- `flock` on `~/backups/openbrain/.reembed.lock` so two sweeps cannot interleave.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reembed.py`:

```python
"""Tests for the reembed sweep."""
import os
import sys
import uuid
from unittest.mock import Mock, call, patch

import pytest


def _cursor_ctx(rowcount=1):
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=ctx)
    ctx.__exit__ = Mock(return_value=False)
    ctx.rowcount = rowcount
    return ctx


class TestZeroVectorRepair:
    def test_zero_vectors_nulled_before_replacement(self):
        from src.cli import reembed
        from src.embedder import EmbeddingError

        row_id = uuid.uuid4()
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [
            [{'id': row_id, 'content': 'text'}],   # zero-vector rows
            [],                                     # NULL rows
        ]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   side_effect=EmbeddingError("ollama down")), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        executed = " ".join(str(c[0][0]) for c in ctx.execute.call_args_list)
        assert "embedding = NULL" in executed, (
            "a zero vector must be NULLed even when replacement fails"
        )
        assert report.zeros_repaired == 1


class TestOptimisticGuard:
    def test_content_changed_is_skipped_not_written(self):
        from src.cli import reembed

        row_id = uuid.uuid4()
        ctx = _cursor_ctx(rowcount=0)   # UPDATE matched nothing
        ctx.fetchall.side_effect = [[], [{'id': row_id, 'content': 'old'}]]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   return_value=[0.1] * 1024), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        assert report.skipped_changed == 1
        assert report.reembedded == 0


class TestAbortBehavior:
    def test_aborts_after_three_consecutive_failures(self):
        from src.cli import reembed
        from src.embedder import EmbeddingError

        rows = [{'id': uuid.uuid4(), 'content': f'c{i}'} for i in range(10)]
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], rows]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   side_effect=EmbeddingError("down")) as mock_embed, \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        assert mock_embed.call_count == 3, "must stop after 3 consecutive failures"
        assert report.reembedded == 0
        assert report.failed == 3
        report.check_invariant()


class TestSkipIds:
    def test_skipped_ids_excluded_from_both_phases(self):
        from src.cli import reembed

        skipped = uuid.uuid4()
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], []]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.acquire_lock'):
            reembed.run_sweep(limit=10, dry_run=False, skip_ids=[str(skipped)])

        for call_args in ctx.execute.call_args_list:
            sql, params = call_args[0][0], call_args[0][1]
            if "SELECT" in sql:
                assert "NOT (id = ANY(" in sql
                assert any(
                    isinstance(pv, list) and str(skipped) in pv for pv in params
                ), "skipped id must reach both selection queries"

    def test_rejects_malformed_skip_ids(self):
        from src.cli.reembed import _parse_skip_ids

        with pytest.raises(ValueError):
            _parse_skip_ids("not-a-uuid")

    def test_parses_and_normalizes(self):
        from src.cli.reembed import _parse_skip_ids

        u = uuid.uuid4()
        assert _parse_skip_ids(f" {u} , ") == [str(u)]
        assert _parse_skip_ids("") == []


class TestReportInvariant:
    def test_dry_run_counts_selected_not_attempted(self):
        from src.cli import reembed

        rows = [{'id': uuid.uuid4(), 'content': 'c'}]
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], rows]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=True)

        assert report.selected == 1
        assert report.attempted == 0
        assert report.unprocessed == 1
        report.check_invariant()

    def test_invariant_holds_after_abort(self):
        from src.cli import reembed

        rows = [{'id': uuid.uuid4(), 'content': f'c{i}'} for i in range(10)]
        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], rows]

        # reembed catches EmbeddingError specifically, so a generic Exception
        # escapes before a report is returned. Take the class from the module
        # under test - the same identity rule as the MCP tests.
        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.create_validated_embedding',
                   side_effect=reembed.EmbeddingError("down")), \
             patch('src.cli.reembed.acquire_lock'):
            report = reembed.run_sweep(limit=10, dry_run=False)

        assert report.aborted
        assert report.attempted + report.unprocessed == report.selected
        report.check_invariant()


@pytest.mark.integration
class TestZeroVectorQueryAgainstLivePgvector:
    """The one thing a mocked cursor cannot establish: that the SQL is valid.

    Deselected by default (see pyproject addopts); run with -m integration.
    Requires the live cluster.
    """

    def test_array_fill_comparison_is_valid_sql(self):
        from src.db.connection import get_db_cursor, get_vector_dim

        dim = get_vector_dim()
        assert dim, "could not read embedding typmod from the live column"

        # Dimension comes from the live typmod, never a literal - the same
        # rule the production query follows.
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) AS n FROM memory
                WHERE embedding = array_fill(0, ARRAY[%s])::vector
                """,
                (dim,),
            )
            row = cursor.fetchone()

        count = row['n'] if isinstance(row, dict) else row[0]
        assert count >= 0


class TestSelectionOrder:
    def test_orders_by_created_at_then_id(self):
        from src.cli import reembed

        ctx = _cursor_ctx()
        ctx.fetchall.side_effect = [[], []]

        with patch('src.cli.reembed.get_db_cursor', return_value=ctx), \
             patch('src.cli.reembed.get_vector_dim', return_value=1024), \
             patch('src.cli.reembed.acquire_lock'):
            reembed.run_sweep(limit=10, dry_run=False)

        null_query = ctx.execute.call_args_list[-1][0][0]
        assert "ORDER BY created_at ASC, id ASC" in null_query
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_reembed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cli.reembed'`.

- [ ] **Step 3: Implement `src/cli/reembed.py`**

```python
"""Re-embed memories with NULL or all-zero embeddings."""
import fcntl
import os
import sys
import uuid
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..db.connection import get_db_cursor, get_vector_dim
from ..embedder import EmbeddingError, create_validated_embedding

LOCK_PATH = Path.home() / "backups" / "openbrain" / ".reembed.lock"
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class ReembedReport:
    """Counts with an invariant that actually holds.

        selected  = rows the queries returned
        attempted = rows the sweep tried to act on
        attempted = reembedded + zeros_repaired + skipped_changed + failed
        selected  = attempted + unprocessed

    In --dry-run, selected > 0 while attempted == 0 and every outcome
    counter stays 0 - which is why `selected` and `attempted` are separate
    fields rather than one `examined`. After an abort, `unprocessed` accounts
    for the rows that were selected but never tried.
    """
    selected: int = 0
    attempted: int = 0
    reembedded: int = 0
    zeros_repaired: int = 0
    skipped_changed: int = 0
    failed: int = 0          # embedder refused; row left as it was
    unprocessed: int = 0     # selected but never attempted (abort or dry-run)
    aborted: bool = False
    dry_run: bool = False

    def check_invariant(self) -> None:
        """Fail loudly on a counting bug rather than reporting nonsense."""
        outcomes = (self.reembedded + self.zeros_repaired
                    + self.skipped_changed + self.failed)
        assert outcomes == self.attempted, (
            f"outcome counters {outcomes} != attempted {self.attempted}"
        )
        assert self.attempted + self.unprocessed == self.selected, (
            f"attempted {self.attempted} + unprocessed {self.unprocessed} "
            f"!= selected {self.selected}"
        )

    def render(self) -> str:
        lines = [
            f"selected:          {self.selected}",
            f"attempted:         {self.attempted}",
            f"re-embedded:       {self.reembedded}",
            f"zeros repaired:    {self.zeros_repaired}",
            f"skipped (changed): {self.skipped_changed}",
            f"failed:            {self.failed}",
            f"unprocessed:       {self.unprocessed}",
        ]
        if self.dry_run:
            lines.append("(dry run - nothing was written)")
        if self.aborted:
            lines.append(
                "ABORTED after 3 consecutive embedder failures. Selection is "
                "oldest-first, so if the same rows fail every run they will "
                "block every future sweep: pass --skip-ids <id,id> to step "
                "over them, or fix their content."
            )
        return "\n".join(lines)


@contextmanager
def acquire_lock():
    """Prevent two sweeps from interleaving."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another reembed is running")
        yield
    finally:
        os.close(fd)


def run_sweep(
    limit: int,
    dry_run: bool,
    skip_ids: Optional[List[str]] = None,
) -> ReembedReport:
    """NULL any zero vectors, then re-embed rows with NULL embeddings.

    skip_ids are excluded from both phases. Selection is oldest-first, so
    three rows that deterministically fail to embed would otherwise be
    re-selected every run and abort every sweep before it reaches
    anything else.
    """
    report = ReembedReport(dry_run=dry_run)
    dim = get_vector_dim()
    skip = [str(i) for i in (skip_ids or [])]

    with acquire_lock():
        # Phase 1: zero vectors -> NULL. A zero vector is never a correct
        # resting state and repairing it must not depend on the embedder being
        # reachable, or a sweep started while Ollama is down aborts having
        # repaired nothing.
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT id, content FROM memory
                WHERE embedding IS NOT NULL
                  AND embedding = array_fill(0, ARRAY[%s])::vector
                  AND NOT (id = ANY(%s::uuid[]))
                ORDER BY created_at ASC, id ASC
                LIMIT %s
                """,
                (dim, skip, limit),
            )
            zero_rows: List[Dict[str, Any]] = list(cursor.fetchall())
        report.selected += len(zero_rows)

        for row in zero_rows:
            if dry_run:
                report.unprocessed += 1
                continue
            report.attempted += 1
            # Guard on the embedding still being zero as well as content
            # unchanged: a concurrent repair must not be clobbered back to
            # NULL. Own cursor context so this row commits on its own.
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE memory SET embedding = NULL
                    WHERE id = %s AND content = %s
                      AND embedding = array_fill(0, ARRAY[%s])::vector
                    """,
                    (str(row['id']), row['content'], dim),
                )
                repaired = cursor.rowcount
            if repaired:
                report.zeros_repaired += 1
            else:
                report.skipped_changed += 1

        # Phase 2: NULL embeddings -> fresh vectors. Rows just NULLed in phase
        # 1 are excluded by id, so they do not consume the limit twice and are
        # picked up by the next sweep instead.
        # Phase-1 rows are excluded so they do not consume the limit twice;
        # the next sweep picks up their fresh NULLs.
        already_seen = {str(r['id']) for r in zero_rows} | set(skip)
        remaining = max(0, limit - report.selected)

        null_rows: List[Dict[str, Any]] = []
        if remaining:
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, content FROM memory
                    WHERE embedding IS NULL
                      AND NOT (id = ANY(%s::uuid[]))
                    ORDER BY created_at ASC, id ASC
                    LIMIT %s
                    """,
                    (list(already_seen), remaining),
                )
                null_rows = list(cursor.fetchall())
        report.selected += len(null_rows)

        consecutive_failures = 0
        for index, row in enumerate(null_rows):
            if dry_run:
                report.unprocessed += 1
                continue
            report.attempted += 1
            try:
                vector = create_validated_embedding(row['content'], dim)
            except EmbeddingError as e:
                consecutive_failures += 1
                report.failed += 1
                print(f"embed failed for {row['id']}: {e}", file=sys.stderr)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    report.aborted = True
                    report.unprocessed += len(null_rows) - index - 1
                    break
                continue

            consecutive_failures = 0
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE memory SET embedding = %s
                    WHERE id = %s AND content = %s
                    """,
                    (vector, str(row['id']), row['content']),
                )
                written = cursor.rowcount
            if written:
                report.reembedded += 1
            else:
                # Content changed between select and update. Writing a vector
                # computed from the old text would be wrong.
                report.skipped_changed += 1

    report.check_invariant()
    return report


def _parse_skip_ids(raw: str) -> List[str]:
    """Parse --skip-ids, rejecting anything that is not a UUID.

    A typo must not silently widen or narrow the sweep, so bad input is an
    error rather than a skipped entry.
    """
    ids: List[str] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        ids.append(str(uuid.UUID(chunk)))   # ValueError on malformed input
    return ids


def reembed_cmd(args: Namespace) -> int:
    """Handle the reembed command."""
    try:
        skip_ids = _parse_skip_ids(getattr(args, 'skip_ids', ''))
    except ValueError as e:
        print(f"Error: --skip-ids contains an invalid UUID: {e}", file=sys.stderr)
        return 2

    try:
        report = run_sweep(
            limit=args.limit, dry_run=args.dry_run, skip_ids=skip_ids
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(report.render())
    return 1 if report.aborted else 0
```

Note: `get_db_cursor` commits per statement via the existing pool's context manager, satisfying the per-row commit requirement.

- [ ] **Step 4: Register the subcommand**

In `src/cli/__init__.py`, after the `report` parser:

```python
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
```

and in the dispatch chain:

```python
        elif args.command == 'reembed':
            from .reembed import reembed_cmd
            return reembed_cmd(args)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_reembed.py -v`
Expected: all 4 PASS.

- [ ] **Step 6: Dry-run against the live database only**

`--dry-run` writes nothing, so it is safe before a backup exists.

Run: `.venv/bin/openbrain reembed --dry-run`
Expected: `selected: 2` (the two known NULL-embedding rows), `attempted: 0`, `unprocessed: 2`, `zeros repaired: 0`, and the `(dry run - nothing was written)` line.

**Do not run the live sweep here.** It mutates the two NULL rows, and no backup
exists until Task 9. Mutating production data before the first verified backup is
exactly the ordering this bundle exists to eliminate — the risk is small (one column,
two rows, guarded by an optimistic content check) but the principle is the whole point
of the work.

The live sweep is deferred to **Task 9, Step 7**, immediately after the first verified
dump exists. Implementation and unit tests are complete here; only the production
mutation moves.

- [ ] **Step 7: Commit**

```bash
git add src/cli/reembed.py src/cli/__init__.py tests/test_reembed.py
git commit -m "feat(cli): add reembed sweep for NULL and zero embeddings

Clears the two rows that have carried NULL embeddings since March, and
is the tool the next embedding-dimension change will need.

Zero vectors are NULLed before any replacement is attempted, so a sweep
started while the embedder is down still repairs them. Updates carry an
optimistic content guard so a row edited mid-sweep is skipped rather
than given a vector computed from stale text."
```

---

### Task 6: Backup filesystem primitives

**Files:**
- Create: `src/cli/backup_store.py`, `tests/test_backup_store.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `BACKUP_DIR: Path` = `~/backups/openbrain`
  - `next_sequence(backup_dir: Path) -> int`
  - `dump_path(backup_dir: Path, seq: int, when: datetime) -> Path`
  - `parse_dump_name(path: Path) -> Optional[Tuple[int, datetime]]`
  - `publish(partial: Path, final: Path) -> None` — no-clobber
  - `write_manifest(path: Path, data: dict) -> None` — atomic
  - `read_manifest(dump: Path) -> Optional[dict]`
  - `manifest_path(dump: Path) -> Path`
  - `sha256_of(path: Path) -> str`
  - `fsync_dir(path: Path) -> None`
  - `select_for_deletion(backup_dir: Path, keep: int, current: Optional[Path]) -> List[Path]`
  - `KEEP_COUNT: int = 60`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backup_store.py`:

```python
"""Tests for backup filesystem primitives."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _make_dump(d: Path, seq: int, minute: int, *, decoded=True, manifest=True):
    """Create a fake dump (+manifest) and return its path."""
    from src.cli import backup_store

    when = datetime(2026, 8, 9, 2, minute, 0, tzinfo=timezone.utc)
    p = backup_store.dump_path(d, seq, when)
    p.write_bytes(b"payload-%d" % seq)
    if manifest:
        backup_store.write_manifest(backup_store.manifest_path(p), {
            "sequence": seq,
            "dump_size": p.stat().st_size,
            "dump_sha256": backup_store.sha256_of(p),
            "verification": {
                "archive_written": when.isoformat(),
                "payload_decoded": when.isoformat() if decoded else None,
            },
        })
    return p


class TestSequence:
    def test_starts_at_one_when_empty(self, tmp_path):
        from src.cli import backup_store
        assert backup_store.next_sequence(tmp_path) == 1

    def test_recovers_from_filenames_when_state_missing(self, tmp_path):
        from src.cli import backup_store
        _make_dump(tmp_path, 7, 0)
        (tmp_path / "sequence").unlink(missing_ok=True)
        assert backup_store.next_sequence(tmp_path) == 8

    def test_recovers_from_corrupt_state(self, tmp_path):
        from src.cli import backup_store
        _make_dump(tmp_path, 4, 0)
        (tmp_path / "sequence").write_text("not a number")
        assert backup_store.next_sequence(tmp_path) == 5

    def test_gap_is_not_an_error(self, tmp_path):
        from src.cli import backup_store
        (tmp_path / "sequence").write_text("11")
        assert backup_store.next_sequence(tmp_path) == 12

    def test_peek_does_not_consume(self, tmp_path):
        """--dry-run must not burn a sequence number."""
        from src.cli import backup_store

        backup_store.ensure_dir(tmp_path)
        assert backup_store.peek_sequence(tmp_path) == 1
        assert backup_store.peek_sequence(tmp_path) == 1
        assert backup_store.next_sequence(tmp_path) == 1
        assert backup_store.peek_sequence(tmp_path) == 2


class TestPublish:
    def test_refuses_to_clobber(self, tmp_path):
        from src.cli import backup_store

        partial = tmp_path / "x.partial.1"
        partial.write_bytes(b"new")
        final = tmp_path / "x.dump"
        final.write_bytes(b"existing")

        with pytest.raises(FileExistsError):
            backup_store.publish(partial, final)

        assert final.read_bytes() == b"existing"

    def test_publishes_and_removes_partial(self, tmp_path):
        from src.cli import backup_store

        partial = tmp_path / "x.partial.1"
        partial.write_bytes(b"data")
        final = tmp_path / "x.dump"

        backup_store.publish(partial, final)

        assert final.read_bytes() == b"data"
        assert not partial.exists()


class TestManifest:
    def test_roundtrip(self, tmp_path):
        from src.cli import backup_store

        dump = _make_dump(tmp_path, 1, 0)
        loaded = backup_store.read_manifest(dump)

        assert loaded["sequence"] == 1
        assert loaded["dump_sha256"] == backup_store.sha256_of(dump)

    def test_rejects_torn_manifest(self, tmp_path):
        from src.cli import backup_store

        dump = _make_dump(tmp_path, 1, 0)
        backup_store.manifest_path(dump).write_text("{ not json")

        assert backup_store.read_manifest(dump) is None


class TestRetention:
    def test_keeps_newest_n_by_sequence(self, tmp_path):
        from src.cli import backup_store

        dumps = [_make_dump(tmp_path, seq, seq) for seq in range(1, 6)]
        doomed = backup_store.select_for_deletion(tmp_path, keep=3, current=None)

        assert set(doomed) == {dumps[0], dumps[1]}

    def test_never_deletes_current_run_output(self, tmp_path):
        """Simulated clock rollback: current dump has the lowest sequence."""
        from src.cli import backup_store

        old = [_make_dump(tmp_path, seq, seq) for seq in range(10, 14)]
        current = _make_dump(tmp_path, 2, 0)

        doomed = backup_store.select_for_deletion(tmp_path, keep=2, current=current)

        assert current not in doomed

    def test_unverified_dump_is_not_eligible(self, tmp_path):
        """archive_written but no payload_decoded: never counted, never pruned."""
        from src.cli import backup_store

        _make_dump(tmp_path, 1, 1)
        _make_dump(tmp_path, 2, 2)
        unverified = _make_dump(tmp_path, 3, 3, decoded=False)

        doomed = backup_store.select_for_deletion(tmp_path, keep=1, current=None)

        assert unverified not in doomed

    def test_manifestless_file_never_deleted(self, tmp_path):
        from src.cli import backup_store

        _make_dump(tmp_path, 1, 1)
        orphan = _make_dump(tmp_path, 2, 2, manifest=False)

        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert orphan not in doomed

    def test_keep_set_never_emptied(self, tmp_path):
        """keep=0 must still leave one dump. A retention bug must not be able
        to delete every backup."""
        from src.cli import backup_store

        dumps = [_make_dump(tmp_path, seq, seq) for seq in range(1, 4)]
        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert len(doomed) == len(dumps) - 1
        assert dumps[-1] not in doomed  # newest by sequence survives

    def test_sha_mismatch_makes_dump_ineligible(self, tmp_path):
        from src.cli import backup_store

        good = _make_dump(tmp_path, 1, 1)
        tampered = _make_dump(tmp_path, 2, 2)
        tampered.write_bytes(b"different content entirely")

        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert tampered not in doomed

    def test_unrecognized_names_ignored(self, tmp_path):
        from src.cli import backup_store

        _make_dump(tmp_path, 1, 1)
        (tmp_path / "random.txt").write_text("x")
        (tmp_path / "openbrain-9-bad.partial.99").write_text("x")

        doomed = backup_store.select_for_deletion(tmp_path, keep=0, current=None)

        assert all(p.name.endswith(".dump") for p in doomed)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_backup_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cli.backup_store'`.

- [ ] **Step 3: Implement `src/cli/backup_store.py`**

```python
"""Filesystem primitives for OpenBrain backups.

Durability rules encoded here:
  - Ordering comes from a monotonic sequence, not timestamps. UTC solves DST
    but is not monotonic across a manual clock change or NTP step.
  - Publication is no-clobber via link()+unlink(). POSIX rename() overwrites
    silently, which would destroy an existing dump.
  - The sequence is reserved (written and fsynced) before use, so a crash
    wastes a number but never reuses one. A reused number would fail
    publication on every later run and wedge backups permanently.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

BACKUP_DIR = Path.home() / "backups" / "openbrain"
SEQUENCE_FILE = "sequence"
KEEP_COUNT = 60

_NAME_RE = re.compile(
    r"^openbrain-(?P<seq>\d+)-(?P<ts>\d{8}T\d{6}\.\d{6})Z\.dump$"
)


def ensure_dir(backup_dir: Path = BACKUP_DIR) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    return backup_dir


def fsync_dir(path: Path) -> None:
    """Make new directory entries durable. Fsyncing files alone is not enough."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    fsync_dir(path.parent)


def parse_dump_name(path: Path) -> Optional[Tuple[int, datetime]]:
    """Return (sequence, timestamp) for a managed dump name, else None."""
    m = _NAME_RE.match(path.name)
    if not m:
        return None
    ts = datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%S.%f").replace(
        tzinfo=timezone.utc
    )
    return int(m.group("seq")), ts


def managed_dumps(backup_dir: Path) -> List[Path]:
    return [p for p in backup_dir.glob("*.dump") if parse_dump_name(p)]


def peek_sequence(backup_dir: Path = BACKUP_DIR) -> int:
    """What next_sequence() would return, without persisting anything.

    --dry-run uses this. Calling next_sequence() would write the incremented
    value, so a dry run would silently consume a number.
    """
    state = backup_dir / SEQUENCE_FILE
    try:
        stored = int(state.read_text().strip())
    except (OSError, ValueError):
        stored = 0

    from_names = 0
    for p in managed_dumps(backup_dir):
        parsed = parse_dump_name(p)
        if parsed:
            from_names = max(from_names, parsed[0])

    return max(stored, from_names) + 1


def next_sequence(backup_dir: Path = BACKUP_DIR) -> int:
    """Reserve the next sequence number, persisting it before returning.

    Recovery: filenames are the durable record, so a missing or corrupt
    state file recomputes rather than halting backups.
    """
    ensure_dir(backup_dir)
    state = backup_dir / SEQUENCE_FILE

    stored = 0
    try:
        stored = int(state.read_text().strip())
    except (OSError, ValueError):
        stored = 0

    from_names = 0
    for p in managed_dumps(backup_dir):
        parsed = parse_dump_name(p)
        if parsed:
            from_names = max(from_names, parsed[0])

    nxt = max(stored, from_names) + 1
    _atomic_write(state, f"{nxt}\n".encode())
    return nxt


SEQ_WIDTH = 6


def dump_path(backup_dir: Path, seq: int, when: datetime) -> Path:
    """Name a dump. Sequence is zero-padded so names sort lexically too."""
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    return backup_dir / f"openbrain-{seq:0{SEQ_WIDTH}d}-{stamp}Z.dump"


def manifest_path(dump: Path) -> Path:
    return dump.with_suffix(dump.suffix + ".json")


def fsync_file(path: Path) -> None:
    """Make a file's data blocks durable.

    Fsyncing only the directory and the manifest makes the *names* durable
    while leaving the archive's payload potentially unwritten - worse than
    no backup, because a power loss then leaves a durable filename and
    manifest pointing at incomplete data that later runs treat as verified.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(partial: Path, final: Path) -> None:
    """Move partial to final without ever overwriting an existing file."""
    fsync_file(partial)          # payload durable before the name exists
    os.link(partial, final)      # raises FileExistsError if final exists
    os.chmod(final, 0o600)
    partial.unlink()
    fsync_dir(final.parent)


def write_manifest(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True).encode())


def read_manifest(dump: Path) -> Optional[dict]:
    """Load a dump's manifest, or None if absent or unparseable."""
    try:
        return json.loads(manifest_path(dump).read_text())
    except (OSError, ValueError):
        return None


def is_eligible(dump: Path) -> bool:
    """A dump counts toward retention only if provably complete and verified.

    'Has a manifest' is not enough: a crash between recording
    archive_written and decoding the payload would otherwise let an
    unverified dump occupy a slot ahead of a verified one.
    """
    manifest = read_manifest(dump)
    if not manifest:
        return False
    if not manifest.get("verification", {}).get("payload_decoded"):
        return False
    try:
        if manifest.get("dump_size") != dump.stat().st_size:
            return False
        if manifest.get("dump_sha256") != sha256_of(dump):
            return False
    except OSError:
        return False
    return True


def select_for_deletion(
    backup_dir: Path,
    keep: int = KEEP_COUNT,
    current: Optional[Path] = None,
) -> List[Path]:
    """Return dumps to prune: eligible ones beyond the newest `keep`.

    The current run's output is always kept by identity. "Never delete the
    newest" is ambiguous under a clock rollback; "never delete what this run
    just created" is not.
    """
    eligible = [p for p in managed_dumps(backup_dir) if is_eligible(p)]
    eligible.sort(key=lambda p: parse_dump_name(p)[0], reverse=True)

    # Never empty the keep set. A retention bug, or a keep of 0, must not be
    # able to leave zero backups on disk.
    effective_keep = max(1, keep)
    doomed = eligible[effective_keep:]
    if current is not None:
        doomed = [p for p in doomed if p != current]
    return doomed


def delete_dump(dump: Path) -> None:
    """Delete a dump and its manifest together."""
    manifest_path(dump).unlink(missing_ok=True)
    dump.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backup_store.py -v`
Expected: all 14 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cli/backup_store.py tests/test_backup_store.py
git commit -m "feat(backup): add filesystem primitives for dumps and retention

Ordering comes from a monotonic sequence rather than timestamps, since UTC
is not monotonic across a clock change or NTP step. Publication uses
link()+unlink() because POSIX rename() silently overwrites an existing
dump. The sequence is reserved and fsynced before use so a crash wastes a
number but never reuses one.

Retention keeps the newest 60 by sequence, counts only dumps whose
manifest confirms payload decode and whose size and SHA-256 match, never
touches files it cannot parse, and always keeps the current run's output."
```

---

### Task 7: Archive verification

**Files:**
- Create: `src/cli/backup.py` (verification half only; Task 8 adds the dump), `tests/test_backup.py`

**Interfaces:**
- Consumes: nothing — this task depends only on `pg_restore` being on PATH
- Produces: `verify_archive(path: Path) -> None` — raises `VerificationError`; `EXPECTED_TOC_ENTRIES`; `class VerificationError(RuntimeError)`

**The real TOC**, captured from this database with `pg_restore -l | grep -vc '^;'` —
**19** object entries (an earlier draft said 20; that was a miscount):

```
ACL - SCHEMA public pg_database_owner
EXTENSION - uuid-ossp            COMMENT - EXTENSION "uuid-ossp"
EXTENSION - vector               COMMENT - EXTENSION vector
FUNCTION public get_memory_stats()
FUNCTION public get_today_memories(integer)
TABLE public memory              TABLE DATA public memory
CONSTRAINT public memory memory_pkey
INDEX public idx_memory_content_fts / idx_memory_created / idx_memory_embedding
      idx_memory_entities / idx_memory_original_date / idx_memory_source / idx_memory_tags
DEFAULT ACL public DEFAULT PRIVILEGES FOR SEQUENCES postgres
DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES postgres
```

- [ ] **Step 1: Write the failing tests**

**Create** `tests/test_backup.py` — this task establishes the file; Task 8 appends to
it. The imports here must cover Task 8's needs too, so it does not have to restate
them:

```python
"""Tests for the backup command."""
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


REAL_TOC = "\n".join([
    "3798; 0 0 ACL - SCHEMA public pg_database_owner",
    "3; 3079 16386 EXTENSION - uuid-ossp ",
    '3799; 0 0 COMMENT - EXTENSION "uuid-ossp" ',
    "2; 3079 16397 EXTENSION - vector ",
    "3800; 0 0 COMMENT - EXTENSION vector ",
    "303; 1255 16725 FUNCTION public get_memory_stats() openbrain",
    "324; 1255 16726 FUNCTION public get_today_memories(integer) openbrain",
    "221; 1259 16727 TABLE public memory openbrain",
    "3791; 0 16727 TABLE DATA public memory openbrain",
    "3643; 2606 16995 CONSTRAINT public memory memory_pkey openbrain",
    "3635; 1259 16996 INDEX public idx_memory_content_fts openbrain",
    "3636; 1259 16997 INDEX public idx_memory_created openbrain",
    "3637; 1259 16998 INDEX public idx_memory_embedding openbrain",
    "3638; 1259 16999 INDEX public idx_memory_entities openbrain",
    "3639; 1259 17000 INDEX public idx_memory_original_date openbrain",
    "3640; 1259 17001 INDEX public idx_memory_source openbrain",
    "3641; 1259 17002 INDEX public idx_memory_tags openbrain",
    "2279; 826 17003 DEFAULT ACL public DEFAULT PRIVILEGES FOR SEQUENCES postgres",
    "2280; 826 17004 DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES postgres",
])


class TestVerification:
    def test_accepts_the_real_toc(self, tmp_path):
        from src.cli.backup import verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")

        with patch('src.cli.backup._pg_restore_list', return_value=REAL_TOC), \
             patch('src.cli.backup._pg_restore_decode') as mock_decode:
            verify_archive(archive)

        mock_decode.assert_called_once()

    def test_rejects_missing_index(self, tmp_path):
        """Completeness: an archive missing one index must fail even though it
        still has TABLE DATA."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")
        partial_toc = "\n".join(
            l for l in REAL_TOC.splitlines() if "idx_memory_tags" not in l
        )

        with patch('src.cli.backup._pg_restore_list', return_value=partial_toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="missing expected"):
                verify_archive(archive)

    def test_prefix_lookalike_is_rejected(self, tmp_path):
        """Exact matching: idx_memory_tags_extra must not pass as
        idx_memory_tags."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")
        toc = REAL_TOC + (
            "\n9999; 1259 99999 INDEX public idx_memory_tags_extra openbrain"
        )

        with patch('src.cli.backup._pg_restore_list', return_value=toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="idx_memory_tags_extra"):
                verify_archive(archive)

    def test_requires_table_data_entry(self, tmp_path):
        """An archive with schema but no row data must fail."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")
        toc = "\n".join(
            l for l in REAL_TOC.splitlines() if "TABLE DATA" not in l
        )

        with patch('src.cli.backup._pg_restore_list', return_value=toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="TABLE DATA"):
                verify_archive(archive)

    def test_rejects_unexpected_toc_entry(self, tmp_path):
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")

        toc = (
            "3791; 0 16727 TABLE DATA public memory openbrain\n"
            "999; 1259 99999 TABLE public surprise_table openbrain\n"
        )
        with patch('src.cli.backup._pg_restore_list', return_value=toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="surprise_table"):
                verify_archive(archive)

    def test_payload_decode_failure_propagates(self, tmp_path):
        """Must use the complete TOC: with exact-set validation, a one-line TOC
        fails on 'missing expected objects' before decode is ever reached, so
        the test would pass for the wrong reason."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")

        with patch('src.cli.backup._pg_restore_list', return_value=REAL_TOC), \
             patch('src.cli.backup._pg_restore_decode',
                   side_effect=VerificationError("corrupt data block")):
            with pytest.raises(VerificationError, match="corrupt"):
                verify_archive(archive)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_backup.py -k Verification -v`
Expected: FAIL — `verify_archive` / `VerificationError` do not exist.

- [ ] **Step 3: Implement verification**

**Create** `src/cli/backup.py` with the module header and the verification half.
Task 8 appends the dump half to this same file, so the imports below deliberately
cover both:

```python
"""openbrain backup - dump, verify, prune."""
import fcntl
import json as jsonlib
import os
import subprocess
import sys
import time
from argparse import Namespace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import psycopg2

from ..db.connection import DatabaseConfig
from . import backup_store as store

READINESS_ATTEMPTS = 5
CONNECT_TIMEOUT_SECONDS = 10
# Sleeps are 2+4+8+16 = 30s of backoff across 5 attempts, plus up to
# 5 * CONNECT_TIMEOUT_SECONDS of connect time: ~80s worst case.
STALE_AFTER_SECONDS = 48 * 3600


class VerificationError(RuntimeError):
    """The archive is not trustworthy."""


# Object identities the dump is expected to contain. An entry outside this
# set means the schema drifted, and the run fails loudly rather than
# half-verifying against stale expectations.
# The 19 object identities this database's dump contains, compared by exact
# equality. Owner and numeric ids are stripped by _toc_identity first.
#
# Derive this set empirically, do not hand-edit it:
#   pg_restore -l dump | grep -v '^;'
# then reduce each line the way _toc_identity does. If verification reports a
# mismatch after an intentional schema change, regenerate the set.
EXPECTED_TOC_ENTRIES = {
    "ACL - SCHEMA public pg_database_owner",
    "EXTENSION - uuid-ossp",
    "EXTENSION - vector",
    'COMMENT - EXTENSION "uuid-ossp"',
    "COMMENT - EXTENSION vector",
    "FUNCTION public get_memory_stats() openbrain",
    "FUNCTION public get_today_memories(integer) openbrain",
    "TABLE public memory openbrain",
    "TABLE DATA public memory openbrain",
    "CONSTRAINT public memory memory_pkey openbrain",
    "INDEX public idx_memory_content_fts openbrain",
    "INDEX public idx_memory_created openbrain",
    "INDEX public idx_memory_embedding openbrain",
    "INDEX public idx_memory_entities openbrain",
    "INDEX public idx_memory_original_date openbrain",
    "INDEX public idx_memory_source openbrain",
    "INDEX public idx_memory_tags openbrain",
    "DEFAULT ACL public DEFAULT PRIVILEGES FOR SEQUENCES postgres",
    "DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES postgres",
}
assert len(EXPECTED_TOC_ENTRIES) == 19


def _pg_restore_list(archive: Path) -> str:
    result = subprocess.run(
        ["pg_restore", "--list", str(archive)], capture_output=True
    )
    if result.returncode != 0:
        raise VerificationError(
            f"pg_restore --list failed: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout.decode(errors="replace")


def _pg_restore_decode(archive: Path) -> None:
    """Decode the whole payload, streamed to /dev/null.

    --list reads mostly the TOC, so it passes on an archive whose later data
    blocks are corrupt, and it reports a TABLE DATA entry even for an empty
    table. Only a full decode exercises every block. Streaming keeps memory
    bounded as volume grows.
    """
    with open(os.devnull, "wb") as devnull:
        result = subprocess.run(
            ["pg_restore", "-f", "-", str(archive)],
            stdout=devnull, stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        raise VerificationError(
            f"payload decode failed: {result.stderr.decode(errors='replace')}"
        )


def _toc_identity(line: str) -> Optional[str]:
    """Reduce a TOC line to its object identity, dropping the numeric ids.

    Input:  '3637; 1259 16998 INDEX public idx_memory_embedding openbrain'
    Output: 'INDEX public idx_memory_embedding openbrain'

    The leading 'N; oid oid ' prefix carries OIDs that differ between dumps,
    so it is stripped. Everything after it - including the owner - is stable
    for this single-role database and is kept, so comparison is exact.
    """
    line = line.strip()
    if not line or line.startswith(";") or ";" not in line:
        return None
    body = line.split(";", 1)[1].strip()
    parts = body.split(None, 2)
    if len(parts) < 3:
        return None
    return parts[2].strip()


def verify_archive(archive: Path) -> None:
    """Verify a dump as far as is possible without restoring it.

    This is the entirety of verification: it proves the archive is
    structurally intact, contains the expected object set, and decodes end
    to end. It does NOT prove the archive restores - nothing here exercises
    CREATE EXTENSION, schema creation, index rebuilds, or the vector(1024)
    column. See the spec's Accepted Risks.
    """
    toc = _pg_restore_list(archive)

    identities = set()
    for line in toc.splitlines():
        ident = _toc_identity(line)
        if ident:
            identities.add(ident)

    if not any(i.startswith("TABLE DATA public memory") for i in identities):
        raise VerificationError(
            "archive has no 'TABLE DATA public memory' entry - it contains no "
            "memory rows"
        )

    # Exact identity comparison, not prefix matching. A prefix test would let
    # a `memory_backup` table pass as `TABLE public memory`, or an
    # `idx_memory_tags_extra` index pass as `idx_memory_tags`.
    unexpected = sorted(identities - EXPECTED_TOC_ENTRIES)
    if unexpected:
        raise VerificationError(
            "unexpected objects in dump TOC (schema drifted; update "
            f"EXPECTED_TOC_ENTRIES): {unexpected}"
        )

    # Completeness matters as much as absence of surprises. Requiring only
    # TABLE DATA would accept an archive missing indexes, the PK, a function,
    # or an extension - structurally incomplete but superficially fine.
    missing = sorted(EXPECTED_TOC_ENTRIES - identities)
    if missing:
        raise VerificationError(
            f"dump TOC is missing expected objects: {missing}"
        )

    _pg_restore_decode(archive)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backup.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Verify against a real archive**

```bash
cd /home/mihai/tools/open-brain
PGPASSWORD=openbrain_local pg_dump -h localhost -p 5433 -U openbrain -Fc \
  -f /tmp/verify-probe.dump openbrain
.venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
from pathlib import Path
from src.cli.backup import verify_archive
verify_archive(Path('/tmp/verify-probe.dump'))
print('archive verified')
"
rm -f /tmp/verify-probe.dump
```
Expected: `archive verified`. Delete the probe — it is a full plaintext copy of all memory content.

- [ ] **Step 6: Commit**

```bash
git add src/cli/backup.py tests/test_backup.py
git commit -m "feat(backup): verify TOC object set and decode full payload

pg_restore --list alone is not enough: it reads mostly the TOC, so it
passes on an archive whose later data blocks are corrupt, and it reports a
TABLE DATA entry even for an empty table. Verification now also decodes
the entire payload streamed to /dev/null.

The TOC is compared by exact set equality against the 19 object identities this
database actually produces, in both directions: an unexpected object fails, and so
does a missing one. Prefix matching would have let a `memory_backup` table pass as
`TABLE public memory`, and requiring only `TABLE DATA` would have accepted an archive
missing every index."
```

---

### Task 8: Snapshot-consistent dump and manifest

**Files:**
- Modify (append to what Task 7 created): `src/cli/backup.py`, `tests/test_backup.py`

**Do not recreate either file.** Task 7 established both, including the module header
and all imports this task needs. Append only.

**Interfaces:**
- Consumes: everything from `backup_store` (Task 6); `verify_archive` and `VerificationError` (Task 7); `DatabaseConfig`
- Produces:
  - `take_dump(backup_dir: Path, seq: int) -> Tuple[Path, dict]` — returns the published dump and its manifest data
  - `pg_env(config) -> dict` — child environment carrying `PGPASSWORD`
  - `snapshot_metadata(conn) -> dict` — row count, dimension, snapshot id

- [ ] **Step 1: Write the failing tests**

**Append** to `tests/test_backup.py` (Task 7 created it with the needed imports):

```python
class TestPgEnv:
    def test_password_in_env_not_argv(self):
        from src.cli import backup

        config = Mock()
        config.password = "s3cret"
        env = backup.pg_env(config)

        assert env["PGPASSWORD"] == "s3cret"

    def test_does_not_mutate_process_env(self):
        from src.cli import backup

        config = Mock()
        config.password = "s3cret"
        backup.pg_env(config)

        assert "PGPASSWORD" not in os.environ


# Every key take_dump reads from snapshot_metadata. An incomplete mock raises
# KeyError deep inside manifest construction.
FAKE_SNAPSHOT_META = {
    "snapshot_id": "0000000A-00000002-1",
    "row_count": 209,
    "embedding_dim": 1024,
    "extensions": [
        {"name": "uuid-ossp", "version": "1.1", "schema": "public"},
        {"name": "vector", "version": "0.8.1", "schema": "public"},
    ],
    "server_version": "18.4",
}


def fake_subprocess_run(argv, **kwargs):
    """Stand in for both pg_dump invocations take_dump makes.

    take_dump calls `pg_dump --version` as well as the dump itself, so a stub
    that assumes every call contains -f raises ValueError on the version call.
    """
    if "--version" in argv:
        return subprocess.CompletedProcess(argv, 0, "pg_dump (PostgreSQL) 18.4\n", "")
    out = argv[argv.index("-f") + 1]
    Path(out).write_bytes(b"PGDMP-fake-archive")
    return subprocess.CompletedProcess(argv, 0, b"", b"")


class TestDumpInvocation:
    def test_argv_has_no_password_and_uses_custom_format(self, tmp_path):
        from src.cli import backup

        with patch('src.cli.backup.subprocess.run', side_effect=fake_subprocess_run) as mock_run, \
             patch('src.cli.backup.snapshot_metadata', return_value=FAKE_SNAPSHOT_META), \
             patch('src.cli.backup.get_db_connection'), \
             patch('src.cli.backup.verify_archive'):
            dump, manifest = backup.take_dump(tmp_path, seq=1)

        dump_argv = next(
            c[0][0] for c in mock_run.call_args_list
            if "--version" not in c[0][0]
        )
        assert "-Fc" in dump_argv
        assert "--snapshot" in dump_argv
        assert not any("openbrain_local" in str(a) for a in dump_argv)
        assert dump.exists()
        assert dump.name.startswith("openbrain-000001-")
        assert manifest["row_count"] == 209
        assert manifest["dump_sha256"]
        assert manifest["verification"]["payload_decoded"]

    def test_verification_failure_leaves_unverified_archive(self, tmp_path):
        """A failed archive must be preserved with a manifest recording why,
        not left as an orphaned .partial."""
        from src.cli import backup, backup_store
        from src.cli.backup import VerificationError

        with patch('src.cli.backup.subprocess.run', side_effect=fake_subprocess_run), \
             patch('src.cli.backup.snapshot_metadata', return_value=FAKE_SNAPSHOT_META), \
             patch('src.cli.backup.get_db_connection'), \
             patch('src.cli.backup.verify_archive',
                   side_effect=VerificationError("corrupt data block")):
            with pytest.raises(VerificationError):
                backup.take_dump(tmp_path, seq=1)

        unverified = list(tmp_path.glob("*.unverified"))
        assert len(unverified) == 1
        assert not list(tmp_path.glob("*.partial*"))

        manifest = backup_store.read_manifest(unverified[0])
        assert manifest["verification"]["payload_decode_failed"]
        assert "corrupt data block" in manifest["verification"]["failure_reason"]
        assert manifest["verification"]["payload_decoded"] is None
        # Recovery metadata must survive a failed verification.
        for key in ("dump_finished", "embedding_dim", "extensions",
                    "server_version", "pg_dump_version"):
            assert manifest[key], f"{key} missing from .unverified manifest"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_backup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cli.backup'`.

- [ ] **Step 3: Append the dump half to `src/cli/backup.py`**

The module header, imports, and constants already exist from Task 7. Add these
functions below the verification code:

```python
def pg_env(config: DatabaseConfig) -> Dict[str, str]:
    """Child environment carrying the password.

    Never argv: command lines are world-readable via /proc. Never a mutation
    of os.environ either - this process may go on to do other things.
    """
    env = dict(os.environ)
    env["PGPASSWORD"] = config.password
    return env


def get_db_connection(config: DatabaseConfig):
    """Connect, retrying briefly.

    A user timer with Persistent=true treats a missed event as handled once
    the service is queued, not once it succeeds. So a run firing right after
    resume, before PostgreSQL is accepting connections, gets no second chance
    until tomorrow unless it retries here.
    """
    delay = 2.0
    last: Optional[Exception] = None
    for attempt in range(READINESS_ATTEMPTS):
        try:
            # connect_timeout bounds each attempt. Without it a single
            # attempt can hang far past the intended total.
            return psycopg2.connect(
                host=config.host, port=config.port, dbname=config.name,
                user=config.user, password=config.password,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except psycopg2.OperationalError as e:
            last = e
            if attempt < READINESS_ATTEMPTS - 1:
                print(f"database not ready, retrying in {delay:.0f}s",
                      file=sys.stderr)
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"database unreachable after {READINESS_ATTEMPTS} attempts: {last}")


def snapshot_metadata(conn) -> Dict[str, Any]:
    """Export a snapshot and read the counts it will contain.

    The row count must come from the same snapshot pg_dump reads, or it
    describes a different database state than the archive. Nothing compares
    it automatically now that restore verification is gone - it exists so
    the runbook's manual check ("this archive should contain exactly N
    rows") is exact rather than approximate.

    The caller must keep this transaction open until pg_dump exits.
    """
    cur = conn.cursor()
    cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cur.execute("SELECT pg_export_snapshot()")
    snapshot_id = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM memory")
    row_count = cur.fetchone()[0]

    cur.execute("""
        SELECT atttypmod FROM pg_attribute
        WHERE attrelid = 'memory'::regclass
          AND attname = 'embedding' AND NOT attisdropped
    """)
    row = cur.fetchone()
    embedding_dim = int(row[0]) if row and row[0] and row[0] > 0 else None

    cur.execute("SELECT extname, extversion, extnamespace::regnamespace::text "
                "FROM pg_extension ORDER BY extname")
    extensions = [
        {"name": r[0], "version": r[1], "schema": r[2]} for r in cur.fetchall()
    ]

    cur.execute("SHOW server_version")
    server_version = cur.fetchone()[0]

    return {
        "snapshot_id": snapshot_id,
        "row_count": row_count,
        "embedding_dim": embedding_dim,
        "extensions": extensions,
        "server_version": server_version,
    }


def _pg_dump_version() -> str:
    out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True)
    return out.stdout.strip()


def take_dump(backup_dir: Path, seq: int) -> Tuple[Path, Dict[str, Any]]:
    """Produce a verified dump plus its manifest data."""
    config = DatabaseConfig.get_instance()
    store.ensure_dir(backup_dir)

    started = datetime.now(timezone.utc)
    final = store.dump_path(backup_dir, seq, started)
    partial = backup_dir / f"{final.name}.partial.{os.getpid()}"

    conn = get_db_connection(config)
    try:
        meta = snapshot_metadata(conn)

        argv = [
            "pg_dump", "-Fc",
            "-h", config.host, "-p", str(config.port),
            "-U", config.user, "-d", config.name,
            "--snapshot", meta["snapshot_id"],
            "-f", str(partial),
        ]
        result = subprocess.run(argv, env=pg_env(config), capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed ({result.returncode}): "
                f"{result.stderr.decode(errors='replace')}"
            )
    finally:
        # Only now is the snapshot no longer needed.
        conn.close()

    os.chmod(partial, 0o600)

    try:
        verify_archive(partial)
    except VerificationError as e:
        # Keep the evidence. An orphaned .partial with no manifest loses the
        # diagnostic entirely, and an unverified archive must never be able to
        # pass as a good backup.
        failed_at = datetime.now(timezone.utc)
        unverified = backup_dir / f"{final.name}.unverified"
        store.publish(partial, unverified)
        # Carry the same metadata a successful manifest gets. It was all
        # collected before verification failed, and a manual recovery from a
        # partially-bad archive needs the dimension and extension versions
        # every bit as much as a clean one does.
        store.write_manifest(store.manifest_path(unverified), {
            "sequence": seq,
            "dump_started": started.isoformat(),
            "dump_finished": failed_at.isoformat(),
            "snapshot_id": meta["snapshot_id"],
            "row_count": meta["row_count"],
            "embedding_dim": meta["embedding_dim"],
            "extensions": meta["extensions"],
            "server_version": meta["server_version"],
            "pg_dump_version": _pg_dump_version(),
            "dump_size": unverified.stat().st_size,
            "dump_sha256": store.sha256_of(unverified),
            "verification": {
                "archive_written": failed_at.isoformat(),
                "payload_decoded": None,
                "payload_decode_failed": failed_at.isoformat(),
                "failure_reason": str(e),
            },
        })
        store.fsync_dir(backup_dir)
        raise

    finished = datetime.now(timezone.utc)
    manifest = {
        "sequence": seq,
        "dump_started": started.isoformat(),
        "dump_finished": finished.isoformat(),
        "snapshot_id": meta["snapshot_id"],
        "row_count": meta["row_count"],
        "embedding_dim": meta["embedding_dim"],
        "extensions": meta["extensions"],
        "server_version": meta["server_version"],
        "pg_dump_version": _pg_dump_version(),
        "dump_size": partial.stat().st_size,
        "dump_sha256": store.sha256_of(partial),
        "verification": {
            "archive_written": finished.isoformat(),
            "payload_decoded": finished.isoformat(),
            "payload_decode_failed": None,
        },
    }

    store.publish(partial, final)
    store.write_manifest(store.manifest_path(final), manifest)
    store.fsync_dir(backup_dir)
    return final, manifest
```

`verify_archive` and `VerificationError` already exist from Task 7, so nothing here is forward-declared. That ordering is deliberate: verification depends on nothing but `pg_restore`, while the dump depends on verification, so building it second means this task's tests can patch a symbol that genuinely exists.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backup.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cli/backup.py tests/test_backup.py
git commit -m "feat(backup): snapshot-consistent pg_dump with sidecar manifest

The row count is read inside the same exported snapshot pg_dump uses, so
the manifest describes exactly what the archive contains rather than a
figure that drifts under concurrent writes.

The password reaches pg_dump through the child environment only, never
argv, where it would be readable via /proc. Connection setup retries with
backoff because a Persistent=true timer treats a missed event as handled
once the service is queued, so a run firing before PostgreSQL is ready
would otherwise wait a full day for another chance."
```

---

### Task 9: CLI modes, locking, retention wiring

**Files:**
- Modify: `src/cli/backup.py`, `src/cli/__init__.py`, `tests/test_backup.py`

**Interfaces:**
- Consumes: `take_dump`, `verify_archive`, `store.select_for_deletion`
- Produces: `backup_cmd(args: Namespace) -> int`; `acquire_lock()`; `status_report(backup_dir) -> dict`

**Mode rules:** exactly one of default / `--verify-only` / `--status`. `--dry-run` valid with default only; `--json` valid with `--status` only. Anything else is a usage error.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup.py`:

```python
class TestCliModes:
    def _args(self, **kw):
        args = Mock()
        args.verify_only = kw.get('verify_only', False)
        args.status = kw.get('status', False)
        args.dry_run = kw.get('dry_run', False)
        args.json = kw.get('json', False)
        return args

    def test_rejects_status_with_dry_run(self):
        from src.cli.backup import backup_cmd
        assert backup_cmd(self._args(status=True, dry_run=True)) == 2

    def test_rejects_status_with_verify_only(self):
        from src.cli.backup import backup_cmd
        assert backup_cmd(self._args(status=True, verify_only=True)) == 2

    def test_rejects_json_without_status(self):
        from src.cli.backup import backup_cmd
        assert backup_cmd(self._args(json=True)) == 2


class TestStatus:
    def test_reports_no_backups(self, tmp_path):
        from src.cli import backup

        report = backup.status_report(tmp_path)

        assert report["dump_count"] == 0
        assert report["newest_verified_age_seconds"] is None
        assert report["healthy"] is False

    def test_reports_fresh_backup(self, tmp_path):
        from src.cli import backup, backup_store
        from datetime import datetime, timezone

        when = datetime.now(timezone.utc)
        dump = backup_store.dump_path(tmp_path, 1, when)
        dump.write_bytes(b"payload")
        backup_store.write_manifest(backup_store.manifest_path(dump), {
            "sequence": 1,
            "dump_size": dump.stat().st_size,
            "dump_sha256": backup_store.sha256_of(dump),
            "row_count": 209,
            "verification": {"archive_written": when.isoformat(),
                             "payload_decoded": when.isoformat()},
        })

        report = backup.status_report(tmp_path)

        assert report["dump_count"] == 1
        assert report["newest_verified_age_seconds"] < 60
        assert report["healthy"] is True


class TestLock:
    def test_second_invocation_fails_fast(self, tmp_path):
        from src.cli import backup

        with patch('src.cli.backup.store.BACKUP_DIR', tmp_path):
            with backup.acquire_lock(tmp_path):
                with pytest.raises(RuntimeError, match="another backup"):
                    with backup.acquire_lock(tmp_path):
                        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_backup.py -k "CliModes or Status or Lock" -v`
Expected: FAIL — `backup_cmd`, `status_report`, `acquire_lock` do not exist.

- [ ] **Step 3: Append the command surface to `src/cli/backup.py`**

`fcntl`, `jsonlib`, `Namespace`, `contextmanager`, and `STALE_AFTER_SECONDS` are
already imported/defined from Task 7. Add these below the existing code:

```python
@contextmanager
def acquire_lock(backup_dir: Path):
    """Serialize runs. A manual invocation can race the timer."""
    store.ensure_dir(backup_dir)
    lock_path = backup_dir / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another backup is running")
        yield
    finally:
        os.close(fd)


def status_report(backup_dir: Path) -> Dict[str, Any]:
    """Machine-readable state. Sub-project 3's health signal consumes this."""
    dumps = store.managed_dumps(backup_dir)
    verified = [p for p in dumps if store.is_eligible(p)]

    newest_age = None
    newest_rows = None
    if verified:
        verified.sort(key=lambda p: store.parse_dump_name(p)[0], reverse=True)
        newest = verified[0]
        manifest = store.read_manifest(newest) or {}
        decoded = manifest.get("verification", {}).get("payload_decoded")
        if decoded:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(decoded)
            newest_age = age.total_seconds()
        newest_rows = manifest.get("row_count")

    # Newest archive age, verified or not: sub-project 3 needs to distinguish
    # "no backup ran" from "a backup ran and failed verification".
    newest_archive_age = None
    all_archives = dumps + list(backup_dir.glob("*.unverified"))
    if all_archives:
        newest_mtime = max(p.stat().st_mtime for p in all_archives)
        newest_archive_age = (
            datetime.now(timezone.utc).timestamp() - newest_mtime
        )

    last_failure = None
    failures = sorted(
        backup_dir.glob("*.unverified"), key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if failures:
        fm = store.read_manifest(failures[0]) or {}
        v = fm.get("verification", {})
        last_failure = {
            "archive": failures[0].name,
            "at": v.get("payload_decode_failed"),
            "reason": v.get("failure_reason"),
        }

    return {
        "backup_dir": str(backup_dir),
        "dump_count": len(dumps),
        "verified_count": len(verified),
        "newest_archive_age_seconds": newest_archive_age,
        "newest_verified_age_seconds": newest_age,
        "newest_row_count": newest_rows,
        "last_failure": last_failure,
        "total_bytes": sum(p.stat().st_size for p in dumps if p.exists()),
        "healthy": newest_age is not None and newest_age < STALE_AFTER_SECONDS,
    }


def _render_status(report: Dict[str, Any]) -> str:
    age = report["newest_verified_age_seconds"]
    age_text = "never" if age is None else f"{age / 3600:.1f}h ago"
    return "\n".join([
        f"backup dir:       {report['backup_dir']}",
        f"dumps:            {report['dump_count']} ({report['verified_count']} verified)",
        f"newest verified:  {age_text}",
        f"last failure:     {report['last_failure'] or 'none'}",
        f"rows in newest:   {report['newest_row_count']}",
        f"total size:       {report['total_bytes'] / 1e6:.1f} MB",
        f"healthy:          {report['healthy']}",
    ])


def backup_cmd(args: Namespace) -> int:
    """Handle the backup command."""
    primary = [bool(getattr(args, 'verify_only', False)),
               bool(getattr(args, 'status', False))]
    if sum(primary) > 1:
        print("Error: --verify-only and --status are mutually exclusive",
              file=sys.stderr)
        return 2
    if getattr(args, 'json', False) and not getattr(args, 'status', False):
        print("Error: --json is only valid with --status", file=sys.stderr)
        return 2
    if getattr(args, 'dry_run', False) and (
        getattr(args, 'status', False) or getattr(args, 'verify_only', False)
    ):
        print("Error: --dry-run is only valid for a normal backup run",
              file=sys.stderr)
        return 2

    backup_dir = store.BACKUP_DIR

    if getattr(args, 'status', False):
        report = status_report(backup_dir)
        print(jsonlib.dumps(report, indent=2) if args.json
              else _render_status(report))
        return 0 if report["healthy"] else 1

    try:
        with acquire_lock(backup_dir):
            if getattr(args, 'verify_only', False):
                verified = [p for p in store.managed_dumps(backup_dir)]
                if not verified:
                    print("Error: no dumps to verify", file=sys.stderr)
                    return 1
                verified.sort(key=lambda p: store.parse_dump_name(p)[0],
                              reverse=True)
                verify_archive(verified[0])
                print(f"verified {verified[0].name}")
                return 0

            if getattr(args, 'dry_run', False):
                # Must not call next_sequence(): it persists the incremented
                # value, so a dry run would consume a sequence number and the
                # first real dump would start at 2.
                seq = store.peek_sequence(backup_dir)
                when = datetime.now(timezone.utc)
                planned = store.dump_path(backup_dir, seq, when)
                print(f"would write {planned.name}")
                # Predict pruning as it will happen *after* the new dump
                # exists, so the preview matches the real run.
                for p in store.select_for_deletion(
                    backup_dir, store.KEEP_COUNT - 1, current=None
                ):
                    print(f"would prune {p.name}")
                return 0

            seq = store.next_sequence(backup_dir)
            dump, manifest = take_dump(backup_dir, seq)
            print(f"wrote {dump.name} ({manifest['row_count']} rows, "
                  f"{manifest['dump_size'] / 1e6:.1f} MB)")

            # Prune only after a written and verified dump. A failed run
            # must never delete a good older one.
            for p in store.select_for_deletion(backup_dir, store.KEEP_COUNT,
                                               current=dump):
                store.delete_dump(p)
                print(f"pruned {p.name}")
            store.fsync_dir(backup_dir)
            return 0

    except (RuntimeError, VerificationError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
```

- [ ] **Step 4: Register the subcommand**

In `src/cli/__init__.py`, after the `reembed` parser:

```python
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
```

and in the dispatch chain:

```python
        elif args.command == 'backup':
            from .backup import backup_cmd
            return backup_cmd(args)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backup.py -v`
Expected: all PASS.

- [ ] **Step 6: Exercise it for real**

```bash
cd /home/mihai/tools/open-brain
.venv/bin/openbrain backup --status          # expect: dumps 0, newest verified never, exit 1
.venv/bin/openbrain backup --dry-run         # expect: "would write openbrain-1-...dump"
.venv/bin/openbrain backup                   # expect: "wrote openbrain-1-... (209 rows, ...)"
.venv/bin/openbrain backup --status          # expect: 1 dump, 1 verified, healthy True, exit 0
.venv/bin/openbrain backup --status --json   # expect: valid JSON
ls -la ~/backups/openbrain/                   # expect: dir 0700, dump+json 0600, sequence file
.venv/bin/openbrain backup --status --dry-run # expect: exit 2, usage error
```

- [ ] **Step 7: Now run the deferred live reembed sweep (from Task 5)**

A verified backup now exists, so the production mutation deferred from Task 5 is safe.

```bash
.venv/bin/openbrain backup --status          # confirm: 1+ verified, healthy
.venv/bin/openbrain reembed                  # expect: re-embedded 2, failed 0
PGPASSWORD=openbrain_local psql -h localhost -p 5433 -U openbrain -d openbrain \
  -tAc "select count(*) from memory where embedding is null;"
```
Expected: `0`.

Then run the marked integration test that proves the zero-vector comparison is valid
SQL against real pgvector. The mocked unit tests cannot establish this, and with no
zero-vector rows present the sweep never exercises it:

```bash
.venv/bin/python -m pytest tests/test_reembed.py -m integration -v
```
Expected: PASS. A failure means `array_fill(...)::vector` is wrong and Task 5's
zero-repair path would break at runtime.

- [ ] **Step 8: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/cli/backup.py src/cli/__init__.py tests/test_backup.py
git commit -m "feat(cli): add openbrain backup with status, verify-only, and pruning

Three mutually exclusive primary modes with defined modifiers; any other
combination is a usage error rather than silently ignored. An flock
serializes the timer against manual runs.

Pruning happens only after a dump is written and verified, keeps the
newest 60 by sequence, and never removes the current run's output.
--status --json is machine-readable so sub-project 3's health signal can
consume it without a redesign."
```

---

### Task 10: systemd units

**Files:**
- Create: `deploy/systemd/openbrain-backup.service`, `deploy/systemd/openbrain-backup.timer`

**Interfaces:**
- Consumes: the `openbrain backup` entrypoint
- Produces: an enabled nightly timer

**Why units live in the repo:** the existing `openbrain-api.service` and the orphan `openbrain-dashboard.service` are untracked, so deleting or changing them is unrecoverable. Tracked files installed by symlink fix that.

- [ ] **Step 1: Create the service unit**

`deploy/systemd/openbrain-backup.service`:

```ini
[Unit]
Description=Open Brain database backup
# No After=postgresql.service: a user manager cannot order against a system
# manager unit, so that line is inert (the existing api unit has it and it
# does nothing). Readiness is handled by retry inside the command.

[Service]
Type=oneshot
WorkingDirectory=/home/mihai/tools/open-brain
Environment=PYTHONPATH=/home/mihai/tools/open-brain
ExecStart=/home/mihai/tools/open-brain/.venv/bin/openbrain backup
```

- [ ] **Step 2: Create the timer unit**

`deploy/systemd/openbrain-backup.timer`:

```ini
[Unit]
Description=Nightly Open Brain database backup

[Timer]
OnCalendar=*-*-* 02:30:00
# Persistent: this laptop sleeps through 02:30 regularly. Without it those
# nights are skipped silently rather than caught up on wake. Note it catches
# up once on resume - it does not replay every missed occurrence.
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

`OnCalendar=02:30` local is DST-safe here: Europe/Bucharest transitions at 03:00 and 04:00, so 02:30 is never skipped or repeated.

- [ ] **Step 3: Install by symlink**

```bash
ln -sf /home/mihai/tools/open-brain/deploy/systemd/openbrain-backup.service \
       ~/.config/systemd/user/openbrain-backup.service
ln -sf /home/mihai/tools/open-brain/deploy/systemd/openbrain-backup.timer \
       ~/.config/systemd/user/openbrain-backup.timer
systemctl --user daemon-reload
systemctl --user enable --now openbrain-backup.timer
```

- [ ] **Step 4: Verify the timer and one real run**

```bash
systemctl --user list-timers openbrain-backup.timer   # expect: next 02:30, Persistent
systemctl --user start openbrain-backup.service
systemctl --user status openbrain-backup.service      # expect: exited, status=0/SUCCESS
journalctl --user -u openbrain-backup.service -n 20 --no-pager
ls -la ~/backups/openbrain/
.venv/bin/openbrain backup --status
```

Expected: a second dump exists with sequence 2, status reports 2 dumps both verified, healthy.

Confirm lingering is on, so the timer fires without an active login:
```bash
loginctl show-user mihai --property=Linger    # expect: Linger=yes
```

- [ ] **Step 5: Commit**

```bash
git add deploy/systemd/openbrain-backup.service deploy/systemd/openbrain-backup.timer
git commit -m "feat(deploy): add tracked systemd units for nightly backup

Units live in the repo and install by symlink, so unlike the existing
untracked api and dashboard units they are version-controlled and their
removal is recoverable.

Persistent=true because this laptop sleeps through 02:30. No
After=postgresql.service: a user manager cannot order against a system
unit, so readiness is handled by retry inside the command instead."
```

---

### Task 11: Preserve the orphan unit, then remove cruft

**Files:**
- Create: `deploy/systemd/attic/openbrain-dashboard.service`
- Delete: `docker-compose.yml`, `Dockerfile`, `web/Dockerfile`, `.dockerignore`, `scripts/backup.sh`, `scripts/healthcheck.sh`, `scripts/startup.sh`, `scripts/setup_db.py`, `~/.config/systemd/user/openbrain-dashboard.service`
- Modify: `config/settings.yaml` and `.env` permissions

**Interfaces:** none

- [ ] **Step 1: Preserve the untracked orphan before deleting it**

```bash
mkdir -p deploy/systemd/attic
cp ~/.config/systemd/user/openbrain-dashboard.service \
   deploy/systemd/attic/openbrain-dashboard.service
```

It is untracked, so without this copy the deletion is unrecoverable.

- [ ] **Step 2: Confirm it is not in use, then remove it**

```bash
systemctl --user is-active openbrain-dashboard.service   # expect: inactive
systemctl --user is-enabled openbrain-dashboard.service  # expect: disabled or not-found
rm ~/.config/systemd/user/openbrain-dashboard.service
systemctl --user daemon-reload
```

- [ ] **Step 3: Delete the Docker path**

```bash
git rm docker-compose.yml Dockerfile web/Dockerfile .dockerignore
```

`docker-compose.yml` declared `pgvector/pgvector:pg16` with user `postgres` / password `openbrain` while reality is system PostgreSQL 18 with `openbrain` / `openbrain_local` — and it mapped host port 5433, so following the README would have collided with the live cluster.

- [ ] **Step 4: Delete the stale operational scripts**

```bash
git rm scripts/backup.sh scripts/healthcheck.sh scripts/startup.sh scripts/setup_db.py
```

- `backup.sh` — port 5432, user `postgres`, plaintext `.sql`, unguarded `find -mtime +7 -delete`, dead S3 hook. Superseded by `openbrain backup`.
- `healthcheck.sh` — port 5432, user `postgres`, queries a nonexistent `memories` table.
- `startup.sh` — `DB_HOST=postgres`, port 5432; Docker-era assumptions.
- `setup_db.py` — creates database and extension, assuming privileges `openbrain` does not have.

`scripts/remember.sh` and `scripts/check_db.py` stay — reviewed and still accurate.

- [ ] **Step 5: Tighten credential file permissions**

```bash
chmod 600 config/settings.yaml .env
stat -c '%a %n' config/settings.yaml .env    # expect: 600 for both
```

Both were `664` under world-traversable directories, so the database password was readable by any local user.

- [ ] **Step 6: Confirm the services still run**

```bash
systemctl --user restart openbrain-api.service openbrain-web.service
sleep 3
curl -s http://localhost:8000/health          # expect: {"status":"healthy"}
.venv/bin/python -m pytest -q                 # expect: all green
```

The permission change must not break the services, which run as the same user.

- [ ] **Step 7: Commit**

```bash
git add -A deploy/systemd/attic
git commit -m "chore: remove stale Docker and operational scripts, tighten credential perms

Each deleted file was a plausible-looking wrong instruction: compose
pointed at pg16 with the wrong credentials and would have collided with
the live cluster on port 5433; backup.sh and healthcheck.sh targeted port
5432 as user postgres, and healthcheck queried a table that does not
exist; setup_db.py assumed privileges this role lacks.

settings.yaml and .env were 664 under world-traversable directories while
holding the database password; both are now 600. The untracked orphan
dashboard unit is preserved under deploy/systemd/attic before removal."
```

---

### Task 12: Documentation

**Files:**
- Modify: `README.md`, `.env.example`, `requirements.txt`, `SPEC.md`, `src/main.py` (docstring)

**Interfaces:** none

- [ ] **Step 1: Fix the MCP server docstring**

`src/main.py` line 1-4 claims FastMCP. The server uses the low-level `mcp.server.Server` API (`from mcp.server import Server`, `@app.list_tools()`, `@app.call_tool()`). Correct the docstring to say so.

- [ ] **Step 2: Rewrite the README's operational sections**

Replace the Docker quick-start with what this machine actually does. Required content:

```markdown
## How This Runs

Two systemd --user services, plus the system PostgreSQL cluster:

| Component | Detail |
| --- | --- |
| API | `openbrain-api.service` → uvicorn on 127.0.0.1:8000 |
| Web | `openbrain-web.service` → Next.js on :3777 |
| Database | System PostgreSQL 18, port **5433**, database/user `openbrain` |
| Embeddings | Ollama `qwen3-embedding:0.6b`, 1024 dimensions |
| Backups | `openbrain-backup.timer`, nightly 02:30, to `~/backups/openbrain/` |

```bash
systemctl --user status openbrain-api openbrain-web openbrain-backup.timer
systemctl --user restart openbrain-api
journalctl --user -u openbrain-backup.service -n 50
```

Unit files are tracked in `deploy/systemd/` and installed by symlink into
`~/.config/systemd/user/`.

## MCP Registration

The MCP server is registered in six agent configurations:

`~/.claude/.claude.json`, `~/.claude-quarticle/.claude.json`,
`~/.codex/config.toml`, `~/.gemini/settings.json`,
`~/.config/opencode/opencode.json`, `~/.config/harness/mcp.json`

## Backups

```bash
openbrain backup            # dump, verify, prune (what the timer runs)
openbrain backup --status   # age of newest verified dump; exit 1 if stale
openbrain backup --status --json
openbrain backup --verify-only
```

Dumps are `pg_dump -Fc` archives in `~/backups/openbrain/`, named
`openbrain-<seq>-<utc>.dump` with a sidecar `.json` manifest recording the
row count, embedding dimension, extension versions, size, and SHA-256.
Retention keeps the newest 60.

**What verification does and does not prove.** Every dump's TOC is checked
against the expected object set and its entire payload is decoded, which
catches truncation, bit-rot, and a broken `pg_dump`. It does **not** prove
the archive restores: nothing exercises `CREATE EXTENSION`, schema
creation, index rebuilds, or the `vector(1024)` column. Restore
verification was deliberately left out to avoid a superuser step.

### Restoring — untested procedure

> These commands have never been run on this machine. Restoring requires
> creating a database and the `uuid-ossp` and `vector` extensions, which
> needs superuser. Expect to debug this the first time.

```bash
sudo -u postgres createdb -O openbrain openbrain_restored
sudo -u postgres psql -d openbrain_restored -c 'CREATE EXTENSION "uuid-ossp"'
sudo -u postgres psql -d openbrain_restored -c 'CREATE EXTENSION vector'
PGPASSWORD=... pg_restore -h localhost -p 5433 -U openbrain \
  -d openbrain_restored --no-owner --no-privileges \
  ~/backups/openbrain/openbrain-<seq>-<utc>.dump
```

Check the restored row count against the manifest's `row_count`.

For embedding-level damage specifically, `openbrain reembed` repairs rows
in place and needs no restore at all.
```

- [ ] **Step 3: Fix `.env.example`**

Change `DB_PORT` to 5433 and `DB_USER` to `openbrain`, and note that `config/settings.yaml` is authoritative for the embedder.

- [ ] **Step 4: Reconcile `requirements.txt`**

`pyproject.toml` is the real dependency source (`pip install -e ".[dev]"`). Either delete `requirements.txt` or reduce it to a pointer at `pyproject.toml`. Do not leave two lists that can drift.

- [ ] **Step 5: Mark `SPEC.md` historical**

Add a header at the top:

```markdown
> **Historical.** This describes the original design, not the current
> system. It is wrong about: ivfflat (the schema uses HNSW), port 5432 (it
> is 5433), FastMCP (the server uses the low-level `mcp.Server` API), and a
> `get_today_memories` SQL function used by the query layer. See `README.md`
> for how the system actually runs.
```

- [ ] **Step 6: Verify the documented commands actually work**

Run every command block in the new README's "How This Runs" and "Backups" sections, except the restore block, which is explicitly untested. Fix any that do not behave as documented.

- [ ] **Step 7: Final full check**

```bash
.venv/bin/python -m pytest -q
.venv/bin/openbrain backup --status
curl -s http://localhost:8000/health
grep -rn "create_embedding(" src/ --include='*.py' | grep -v "create_validated_embedding" | grep -v "^src/embedder/__init__.py:"
```
Expected: tests green, status healthy, API healthy, no unvalidated embedding call sites.

- [ ] **Step 8: Commit**

```bash
git add README.md .env.example requirements.txt SPEC.md src/main.py
git commit -m "docs: describe how the system actually runs

The README documented a Docker quick-start that would have started a
second, empty database on the live cluster's port. It now describes the
systemd units, PostgreSQL 18 on 5433, the six MCP registration paths, and
the backup commands.

The restore procedure is marked explicitly as untested, because with no
verification database there is no privilege-free way to rehearse it - the
honest thing is to say so where a reader will see it. SPEC.md is marked
historical rather than maintained as a second architecture document."
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: CLI modes → 9; snapshot dump → 7;
files, manifest, payload fsync, sequence durability → 6, 8; verification and its
failure path → 7, 8; retention → 6, 9; concurrency → 6, 9, 5; scheduling → 10;
Component 2 → 2, 3, 4, 5; Component 3 → 1 plus tests in every task; Component 4 → 11,
12; error handling → 5, 7, 8, 9; accepted risks → documented in 12's README.

**Corrections applied after the plan review.** Recorded because several were factual
errors, not judgment calls:

| Was wrong | Now |
| --- | --- |
| Tests imported `cli.store` — raises `ImportError` (verified) | `src.cli.store`, patched at the use site, `sys.path` only in `conftest.py` |
| Task 3 renamed `_validate_embedding_dim` at 2 of **3** sites, missing `search_memories:170` → `NameError` in vector search | All three sites, plus a regression test on the vector-search path |
| Spec and plan said the dump TOC has 20 entries | **19**, counted with `pg_restore -l \| grep -vc '^;'` |
| TOC matching was prefix-based and only required `TABLE DATA` | Exact set equality both directions: unexpected **and** missing objects fail |
| `--dry-run` called `next_sequence()`, consuming a sequence number | `peek_sequence()`, which persists nothing |
| Verification failure left an orphaned `.partial` with no manifest | Published as `.unverified` with a manifest recording the reason; surfaced in `--status` |
| `publish()` never fsynced the payload | `fsync_file()` before `link()` |
| `select_for_deletion` could delete every dump at `keep=0` | Floor of 1; test asserts it |
| `reembed` held one cursor for the whole sweep — one commit, so a late error rolls back every earlier repair | One cursor context per row |
| The dump task's test omitted manifest fields and broke on `pg_dump --version` | Complete `FAKE_SNAPSHOT_META` and a stub handling both invocations |
| Task 5 mutated live data before any backup existed | Live sweep deferred to Task 9 Step 7, after the first verified dump |
| Readiness described as "~2 min cap"; actually 30s, with no `connect_timeout` | 30s backoff + `connect_timeout=10`, ~80s worst case, stated accurately |
| The raw-call-site grep flagged the helper's own internal call | Excludes `src/embedder/__init__.py` wholesale |
| The dump task was conditional on a review that timed out | Resolved: snapshot machinery stays, verified working against the live cluster |
| `reembed` could clobber a concurrently repaired vector, double-count its limit, and report inaccurate totals | Zero-guard in the `WHERE`, phase 2 excludes phase 1 ids, `selected`/`attempted` split with a `check_invariant()` assertion, `--skip-ids` fully plumbed through both queries |
| Sequence unpadded despite the spec requiring padding | 6 digits, width in one constant |
| `import src.main` fails outright (verified) | `tests/conftest.py` puts `src/` on the path |

**Type consistency.** `create_validated_embedding(text, expected_dim)` matches across
Tasks 2, 4, 5. `update_memory(..., embedding=UNSET, ...)` matches Tasks 3 and 4.
`store.select_for_deletion(backup_dir, keep, current)` matches Tasks 6 and 9.
`store.peek_sequence` / `store.next_sequence` match Tasks 6 and 9. `verify_archive(path)`
raising `VerificationError` is defined in Task 7 and consumed by Tasks 8 and 9. `ReembedReport` fields match its
implementation and `render()`.

**Known wart, deliberately preserved:** `get_vector_dim()` is imported per-module in
whichever idiom that module already uses. Unifying the repo's import strategy would
touch every module for no functional gain, and `conftest.py` makes the mixed style
testable.
