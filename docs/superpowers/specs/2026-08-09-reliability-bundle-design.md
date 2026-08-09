# Reliability Bundle — Design

Date: 2026-08-09
Status: draft, revision 8 (Codex rounds 1–4 + 2 plan-review rounds; restore verification removed by decision)
Scope: sub-project 1 of 4 in the OpenBrain improvement sequence

## Context

OpenBrain is the durable memory store for every LLM agent on this laptop. Its MCP
server is registered in six agent configurations (`~/.claude`, `~/.claude-quarticle`,
`~/.codex`, `~/.gemini` / antigravity, `~/.config/opencode`, `~/.config/harness`).

Observed state on 2026-08-09:

| Fact | Value |
| --- | --- |
| Memories | 209 (207 embedded, 2 NULL, 0 zero-vectors) |
| Database size | 4.8 MB |
| Date range | 2026-03-13 → 2026-07-27 (nothing written in 13 days) |
| Runtime | `openbrain-api.service` (127.0.0.1:8000), `openbrain-web.service` (\*:3777), both systemd --user, `Linger=yes` |
| Database | System PostgreSQL 18, port 5433, db/user `openbrain`, `rolsuper=false`, `rolcreatedb=false` |
| Extensions | `uuid-ossp` 1.1 and `vector` 0.8.1, both in schema `public`; `vector.control` does **not** declare `trusted = true` |
| Dump TOC | **19** object entries: 2 extensions, 2 extension comments, 1 schema ACL, 2 functions, table, table data, PK, 7 indexes, **2 `DEFAULT ACL … postgres` entries** |
| Credential files | `config/settings.yaml` and `.env` are mode **664** — world-readable, containing the DB password |
| Backups | None on disk. `scripts/backup.sh` exists but has never run and is misconfigured |
| Test suite | `tests/test_core.py`, **46 passed** under the venv interpreter; `pytest` absent from `.venv` |
| Timezone | Europe/Bucharest (EEST, +0300), observes DST |
| Free disk | 782 GB on `/` |

Sub-projects 2–4 (retrieval quality; auto-capture via a Claude Code Stop hook;
transcript-search MCP) follow in that order and are out of scope here.

### Why this goes first

Sub-project 3 multiplies write volume into a database with no backup. Sub-project 2
rewrites the search path the test suite guards. Both are safer once this lands.

## Goals

1. A nightly backup exists whose archive integrity is verified end to end, with
   machine-readable status. **Not** proven restorable — see Accepted Risks.
2. No code path writes a corrupt or stale embedding.
3. The test suite runs in the venv and on push.
4. The repository describes how the system actually runs, with no competing instructions.

## Non-goals

- Off-machine backup copies (explicit user decision; see Accepted Risks).
- **Restore verification of any kind** (explicit user decision: no superuser step).
  This removes the prepared verification database, the TOC-filtered restore, the
  restored-schema comparison, and the PostgreSQL advisory lock that protected the
  shared database. See Accepted Risks for what that costs.
- Backup failure alerting — deferred to sub-project 3, which owns one health signal
  covering stale backups and stalled capture. This bundle only exposes the data.
- Any change to search ranking, tagging, or the MCP tool surface.
- Batch embedding in the importer (see Component 2).
- Chunking, HNSW/`ef_search` tuning. Not justified at 209 rows.

---

## Component 1 — Backup

### CLI

`openbrain backup`, in `src/cli/backup.py`, registered in `src/cli/__init__.py`.

Exactly one primary mode per invocation, mutually exclusive:

| Mode | Meaning |
| --- | --- |
| *(default)* | Dump, verify, write manifest, prune |
| `--verify-only` | Re-verify the newest existing dump; produce no new dump |
| `--status` | Report state; mutate nothing |

Modifiers: `--dry-run` prints planned actions (valid with default only); `--json`
emits machine-readable output (valid with `--status` only). Any other combination is
rejected with a usage error rather than silently ignored.

`--deep-verify` is gone along with restore verification.

Credentials come from `src.db.connection.DatabaseConfig`, not a raw YAML read, so
the backup honors the same environment-variable overrides as the running
services.

### Snapshot-consistent dump

The manifest's row count is read from the *same snapshot* `pg_dump` uses, so the
recorded figure is exactly what the archive contains rather than an approximation.
A count taken outside the dump's snapshot drifts under concurrent writes — a
transaction that began before the dump but commits after its snapshot is absent from
the archive yet present in a naive count.

With restore verification dropped, nothing compares this number automatically. It is
kept because it is what makes the runbook's manual restore check meaningful: "this
archive should contain exactly N rows" is actionable, while an approximate figure
invites chasing a phantom discrepancy. The cost is a dozen lines.

Procedure:

1. Open a connection; `BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ`. Isolation
   must be established **before** the transaction's first query.
2. `SELECT pg_export_snapshot()` → snapshot id.
3. In that same transaction, read `count(*) FROM memory` and the `embedding` column
   typmod from `pg_attribute`. Both must execute **on the exporting transaction's
   connection**, not through the cached `connection.get_vector_dim()` pool
   connection, or they describe a different snapshot than the dump.
4. Run `pg_dump -Fc --snapshot=<id>` against the same database while the
   transaction is still open.
5. Commit only after `pg_dump` has exited. Holding it open for the whole dump
   removes any question about when the snapshot is imported.

Never a hardcoded 1024: the dimension always comes from the live column typmod.

### Files

- `~/backups/openbrain/openbrain-<seq>-YYYYMMDDTHHMMSS.ffffffZ.dump`, where
  `<seq>` is a **monotonic sequence number** read and incremented under the lock
  from a small state file in the backup directory, zero-padded to **6 digits**
  (`000001`). A fixed width keeps names sorting lexically as well as numerically;
  6 digits is ~2700 years of nightlies. Ordering logic parses the integer and does
  not depend on the padding.
- The sequence, not the timestamp, defines ordering. UTC solves DST but is **not**
  monotonic across a manual clock change or NTP step, so timestamps alone cannot
  answer "which dump is newest" — and under a clock rollback a timestamp-ordered
  keep-set could exclude a dump during the very run that created it. The timestamp
  stays in the name because it is what a human reads.
- Microsecond resolution additionally avoids same-name collisions: this database
  dumps and payload-verifies in well under a second, so two serialized runs can
  complete within the same UTC second.
- **Publication is no-clobber.** `O_EXCL` on the partial file does not stop a later
  `rename()` from replacing an existing final path — POSIX `rename` overwrites
  silently. Publication instead uses `link()` from the completed same-filesystem
  partial to the final name, which fails if the target exists, followed by
  `unlink()` of the partial. (`renameat2(RENAME_NOREPLACE)` is the Linux-specific
  equivalent; `link` is chosen for being straightforwardly available from Python.)
- **The dump's own payload is `fsync`ed before publication.** Fsyncing the directory
  and the manifest makes the *names* durable while leaving the archive's data blocks
  potentially unwritten — which is worse than no backup, because a power loss then
  yields a durable filename and a durable manifest pointing at incomplete data that
  every later run treats as verified.
- After both the dump and its manifest are published, the **backup directory itself
  is `fsync`ed** as well. Fsyncing the files alone leaves the new directory entries
  vulnerable to sudden power loss.
- A sidecar manifest `…​.dump.json` per dump: dump start/end (UTC), snapshot id,
  row count, embedding dimension, extension names/versions/schemas, server version,
  pg_dump version, dump size, dump **SHA-256**, and verification state.
- The three schema definition lists from earlier revisions are **dropped** — they
  existed solely as the authoritative side of the restored-schema comparison, and
  with no restore there is nothing to compare against. Extension
  names/versions/schemas are kept because a human performing a real restore needs
  to know that the target requires `uuid-ossp` 1.1 and `vector` 0.8.1.
- The manifest is written atomically — temporary file, `fsync`, `rename` — so a
  torn or stale sidecar can never supply a wrong row count, dimension, or
  verification state. Size and SHA-256 bind the manifest to its dump; a mismatch
  invalidates the pair.
- Directory mode `0700`, files `0600` — the dump is a plaintext-recoverable copy
  of all memory content.
- Written as `…​.partial.<pid>`, then **published by `link()` + `unlink()` as
  described above — never by `rename()`** — so an interrupted run leaves only a
  partial file, never a truncated file that later looks valid, and a completed run
  can never overwrite an existing dump.

### Sequence state durability

Retention ordering now depends on the sequence file, so it needs the same care as
the dumps:

- The incremented sequence is written atomically (temp file, `fsync`, `link`/replace)
  and `fsync`ed **before** that number is used for a dump filename. Reserving first
  means a crash can waste a number but never reuse one — a reused number would fail
  no-clobber publication on every subsequent run, wedging backups permanently.
- **Gaps are valid.** A run that reserves N and then fails leaves N unused. Nothing
  treats a gap as an error.
- **Recovery from a missing or corrupt state file:**
  `max(sequences parsed from all managed dump filenames, stored sequence if it
  parses) + 1`. The filenames are the durable record, so a lost state file degrades
  to a recalculation rather than halting all future backups.
- The backup directory is `fsync`ed after the state file is published.

### Verification state

Verification outcomes are recorded in the manifest, not inferred from file
existence, because a crash can leave an archive on disk that was never checked:

```
archive_written | payload_decoded | payload_decode_failed
```
each with a UTC timestamp. `--status` reports the age of the newest dump whose
`payload_decoded` succeeded — not merely the newest file present.

### Verification (every run, no privileges)

1. `pg_restore --list` — assert the TOC contains a `TABLE DATA public memory`
   entry, and compare the TOC against an **enumerated set of expected identities
   by exact equality, in both directions**: an unexpected object fails the run, and
   so does a *missing* one. The real TOC has **19** object entries, not three:
   `EXTENSION uuid-ossp`, `EXTENSION vector`, their two `COMMENT - EXTENSION`
   entries, `ACL - SCHEMA public pg_database_owner`, two `FUNCTION` entries,
   `TABLE public memory`, `TABLE DATA public memory`, `CONSTRAINT memory_pkey`,
   seven `INDEX` entries, and two `DEFAULT ACL … postgres` entries. Anything
   outside the allowlist means the schema drifted and the reset/filter logic is
   stale — the run fails loudly rather than half-verifying.
2. `pg_restore -f /dev/null` — decode the **entire payload**, streamed, never
   buffered in memory. `--list` alone reads mostly the TOC and would pass on an
   archive whose later data blocks are corrupt, and it reports a `TABLE DATA`
   entry even for an empty table. Streaming to `/dev/null` keeps this bounded as
   volume grows under sub-project 3.

**On failure**, the archive is published to `…​.unverified` rather than to its
final `.dump` name, a manifest is still written recording `payload_decode_failed`
with the reason, and the run exits non-zero without pruning. An unverified archive
must be inspectable, must never count toward retention, and must never be mistaken
for a good backup — leaving it as an orphaned `.partial` with no manifest loses the
diagnostic entirely.

**This is the entirety of verification.** Together the two steps prove the archive
is structurally intact, contains the expected object set, and can be decoded end to
end without error. They do **not** prove it restores into a working database —
nothing here exercises `CREATE EXTENSION`, schema creation, index rebuilds, or the
`vector(1024)` column. Restore verification was dropped by decision; see Accepted
Risks, where the consequence is stated plainly.

### Retention

**Keep the newest 60 dumps by sequence number.** One flat rule, no tiers.

The two-tier design (14 daily plus 8 weekly) existed because the weekly tier was
keyed on deep verification. With deep verify gone there is nothing to tier on, and
tiering for its own sake would buy only edge cases. At well under a megabyte per
dump against 782 GB free, 60 nightlies is roughly two months of history for under
60 MB — more coverage than the tiered scheme gave, with none of its keep-set
arithmetic.

- **The current run's dump is always added to the keep-set by identity**,
  independent of the ordering rule. "Never delete the newest" is ambiguous under a
  clock rollback; "never delete the dump this run just created" is not, and it is
  the invariant that actually matters — a run must never prune its own output.
- A dump counts toward the 60 — and is eligible for deletion — **only** if it has a
  valid manifest whose size and SHA-256 match the file **and** whose
  `payload_decoded` state is successful. "Has a manifest" is not enough: a crash
  between recording `archive_written` and decoding the payload would otherwise
  leave an unverified dump occupying a slot ahead of a genuinely verified one.
- `.partial.*`, `.unverified`, malformed names, manifest-less files, and anything
  unrecognized are never auto-deleted — retention must not be able to remove a
  file it does not understand.
- Never delete the newest dump, and never let the eligible-keep set reach zero.
- Prune only after the new dump is written **and** payload-verified. A failed run
  never deletes a good older dump.
- Deleting a dump deletes its manifest with it.

### Concurrency

A single `flock` on `~/backups/openbrain/.lock`, held for the whole run. A second
invocation exits non-zero immediately with "another backup is running" rather than
blocking. This covers the timer racing a manual run, and protects the sequence
state, the manifests, and pruning. PID-suffixed `.partial` names are
belt-and-braces.

The PostgreSQL session-level advisory lock from earlier revisions is **dropped
along with deep verify.** It existed to stop a second Unix user — who would get a
different `$HOME` and therefore a different lock file — from clobbering the shared
verification database. With no shared database, all remaining contention is over
files under `~/backups`, where `flock` is sufficient and a cross-user writer would
need write access to this user's home directory anyway.

Related: `config/settings.yaml` and `.env` are currently mode **664** under
world-traversable directories, so the database password is readable by any local
user. Both are tightened to **0600** as part of this bundle. On a single-user
laptop this is defense in depth, but a world-readable file holding a database
password is worth fixing on sight.

`reembed` holds its own `flock` so two sweeps cannot interleave, but it
deliberately does **not** contend with the backup lock — its concurrent writes are
already handled correctly by the exported snapshot, so serializing it against
backups would only reduce availability for no correctness gain.

### Scheduling

Unit files live **in the repository** at `deploy/systemd/`, installed by symlink
into `~/.config/systemd/user/`, so they are version-controlled and their removal
is genuinely reversible.

- `openbrain-backup.service` — `Type=oneshot`, `WorkingDirectory=/home/mihai/tools/open-brain`,
  `ExecStart=…/.venv/bin/openbrain backup`, `Environment=PYTHONPATH=…`.
- `openbrain-backup.timer` — `OnCalendar=*-*-* 02:30:00`, `Persistent=true`,
  `RandomizedDelaySec=300`, `WantedBy=timers.target`.

Install: `systemctl --user daemon-reload && systemctl --user enable --now openbrain-backup.timer`.

`Linger=yes` is set for this user (verified), so user timers fire without an
active login. `OnCalendar=02:30` local avoids DST hazard: Romania transitions at
03:00 and 04:00, so 02:30 is never skipped or repeated.

No `After=postgresql.service` — a **user** manager cannot order against a system
unit, so the existing `openbrain-api.service` line is ineffective and copying it
would spread the mistake. Instead the command performs a bounded readiness retry
(5 attempts, exponential backoff, ~2 min cap) before failing, which also covers
the real failure mode: `Persistent=true` considers a missed event handled once the
service is *queued*, not once it *succeeds*, so a post-resume run that hits an
unavailable cluster gets no automatic second chance until tomorrow.

---

## Component 2 — Embedding integrity

The originally-suspected defect was the wrong target. Verified current state:

| Path | Behavior | Verdict |
| --- | --- | --- |
| MCP `memory_store` (`main.py:756`) | Catches failure, stores NULL, reports `embedded: false` | Already correct |
| MCP `memory_update` (`main.py:824`) | Sets `embedding` only on success; `update_memory` skips `None` (`queries.py:379`) → **old vector kept on new content** | **Live bug** |
| REST `PUT /memories/{id}` (`api/main.py:233`) | Passes content/tags/source/importance only — **never re-embeds**, never re-extracts entities or tags | **Live bug, worse** |
| `OllamaEmbedder.embed_batch` (`embedder/__init__.py:339`) | Substitutes `[0.0] * dim` on failure | Real, but **unreachable** |
| `_validate_embedding_dim` (`queries.py:34`) | Checks length only | Gap: accepts all-zero vectors |

`embed_batch` has **no production caller**: `importer.py:12` imports
`create_embeddings` but never calls it, and every import path inserts
`embedding=None` (lines 69, 114, 157) under the comment "Generate in batch for
efficiency" — batching was never implemented. Only the Ollama provider has the
zero-substitution; the others raise.

So the stale-embedding bug on the update paths is the highest-value correctness
fix here, and it is the one that actually violates Goal 2 today. The web UI's
memory editor uses the REST endpoint, so this is a path in real use.

### Changes

1. **Explicit-NULL sentinel, plus a enforced invariant.** `update_memory` currently
   cannot distinguish "leave the embedding alone" from "clear it", because both are
   `None`. Introduce a module-level `UNSET = object()` default for the `embedding`
   parameter; `None` then means `SET embedding = NULL`. Content and embedding are
   written in the same single `UPDATE`.

   The sentinel alone is **not** sufficient, and an earlier claim that content and
   embedding "can never diverge" was wrong: `update_memory(id, content="changed")`
   with `embedding` left `UNSET` would still write new content over an old vector.
   So the invariant is enforced at the function boundary — **if `content` is
   supplied and `embedding` is `UNSET`, the call raises.** Callers must decide
   explicitly: a fresh vector, or `None` to clear. Updates that touch only tags,
   source, or importance may continue to leave `embedding` unset, since content did
   not change.

   Rejecting is chosen over silently auto-NULLing because auto-NULL would discard a
   valid embedding whenever a caller intended to supply one a moment later, and
   would hide the mistake. Handler-level correctness must not be the only thing
   standing between a future caller and a stale vector.
2. **MCP update:** on re-embed failure, pass `embedding=None` — clear the stale
   vector. The row stays findable by full-text search and becomes sweepable by
   `reembed`. Report `reembedded: false` as it already does.
3. **REST update:** re-embed on content change with the same semantics, and
   re-extract entities and tags, matching the MCP handler. Also switch the call
   to keyword arguments — it currently relies on positional order into an
   eight-parameter signature.
4. **Zero-vector rejection at two levels.** Rename `_validate_embedding_dim` →
   `_validate_embedding` and reject all-zero vectors there, so every insert and
   update is covered for every provider. Length-only validation currently lets a
   1024-zero response through both `memory_store` and `reembed` — meaning
   `reembed` could "repair" a zero vector by writing another one.

   **But the database layer cannot be the only check.** It raises from inside
   `insert_memory`/`update_memory`, which run *after* the callers' embedding `try`
   blocks have already completed — so a provider returning a zero vector would make
   the store **fail outright** instead of storing NULL, converting a degraded
   embedding into a lost memory. That is the exact failure mode this bundle exists
   to prevent.

   Fixing this only in the two MCP handlers would leave the promise false
   everywhere else. There are **twelve** current call sites, plus a thirteenth the
   redesigned REST update adds:

   | Site | Required degradation |
   | --- | --- |
   | `cli/store.py:30`, `api/main.py:162` (REST create) | Store with NULL embedding |
   | `connectors/gmail.py:209`, `file_watcher.py:140`, `claude_code.py:138`, `whatsapp.py:112`, `telegram.py:108` | Import row with NULL embedding |
   | `api/main.py:296`, `cli/search.py:20`, **`main.py:718` (MCP search)** | **Degrade to text-only search**, never raise — an unusable query vector must not break search |
   | `main.py` store and update (via `asyncio.to_thread`) | Store/set NULL, report `embedded: false` |

   `main.py:718` already catches exceptions and falls through to text-only search,
   but it calls raw `create_embedding`, so a zero vector would pass unchecked into
   `search_memories` and then raise from the database-layer backstop — turning a
   degraded query into a failed search.

   So validation is centralized in one new helper,
   `create_validated_embedding()`, which embeds, checks dimension and
   non-zeroness, and raises a distinct `EmbeddingError`. Every site above calls it
   inside its existing `try` and applies the degradation in its column. The
   database-layer check stays as a backstop against callers that bypass the helper.
5. **`embed_batch`:** return `None` per failed item instead of zeros
   (`List[Optional[List[float]]]`), and log to `stderr` not `stdout`. Cheap, keeps
   the API honest. Also delete the unused `create_embeddings` import from
   `importer.py`. Importer batching stays out of scope — designing it properly
   needs batch-failure handling, result-length validation, and index preservation
   across all four providers, which is its own piece of work and buys nothing
   while no connector is scheduled.

Note on the earlier ranking rationale: zero vectors are *not* indexed by cosine
HNSW and NaN sorts deterministically in PostgreSQL, so the "undefined ranking"
framing was wrong. The genuine hazard is duplicate detection (`queries.py:828`),
where `1.0 - (a.embedding <=> b.embedding) >= threshold` can behave unexpectedly
under PostgreSQL's NaN comparison semantics.

### `openbrain reembed`

`src/cli/reembed.py`. Contract, stated explicitly:

- `--limit N` bounds **rows examined**, applied to zero-vector repair and NULL
  sweeping together.
- Selection order `created_at ASC, id ASC` — `id` is the stable tie-breaker.
- Each row commits individually; a later failure never rolls back earlier
  successes.
- **Optimistic content guard:** the `UPDATE` carries `WHERE id = %s AND content =
  %s` against the content that was embedded. If the row changed underneath, the
  update affects zero rows and is reported as skipped rather than attaching a
  vector computed from stale content.
- Aborts after **3 consecutive** embedder failures, exits non-zero, leaves the
  rest for the next run.
- Zero-vector detection compares against `array_fill(0, ARRAY[<dim>])::vector`
  with `<dim>` from the live column typmod, not settings — otherwise a dimension
  migration makes the query error out.
- **A detected zero vector is NULLed first, before any replacement is attempted**,
  each under the optimistic content guard. Waiting for a successful replacement
  would leave poisoned rows poisoned whenever the embedder is unavailable — and
  since the command aborts after 3 consecutive failures, a sweep that starts while
  Ollama is down would otherwise exit having repaired nothing. NULL is a correct
  resting state; a zero vector is not.
- Holds its own `flock` so two invocations cannot interleave.
- Reports counts with an invariant that holds in every mode:
  `attempted = reembedded + zeros_repaired + skipped_changed + failed`, and
  `selected = attempted + unprocessed`. `selected` and `attempted` are separate
  because a dry run selects rows without attempting any, and an abort leaves
  selected-but-unattempted rows — a single `examined` counter cannot represent
  either honestly. Asserted at the end of every sweep.
- `--skip-ids` excludes specific ids from both phases. Selection is oldest-first, so
  three rows that deterministically fail to embed would otherwise be re-selected
  every run and abort every sweep before it reached anything else.

Clears the 2 existing NULL rows. Left manual in this bundle.

---

## Component 3 — Tests

The suite's status is now known, not unknown: **46 passed** under
`.venv/bin/python`. It fails under system Python only because `mcp` and
`psycopg2` are absent there. So this component adds capability rather than
repairing breakage.

- `.venv/bin/pip install -e ".[dev]"` — pytest, pytest-asyncio, black, ruff are
  already declared in `pyproject.toml`.
- Add `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, `asyncio_mode = "auto"`,
  and an `integration` marker deselected by default.
- Unit tests (mocked, in the existing suite's style):
  - `embed_batch` yields `None`, never zeros, on a mocked failure.
  - `_validate_embedding` rejects an all-zero vector on both insert and update.
  - `update_memory` distinguishes `UNSET` from `None`, and emits `embedding = NULL`
    for the latter.
  - MCP update clears the embedding when re-embedding raises.
  - REST update re-embeds and re-tags on content change.
  - `reembed` optimistic guard: a content change between select and update yields
    "skipped", not a write.
  - Retention selection over synthetic manifests: the flat keep-newest-60 rule,
    unparseable and unmanifested files never selected, the current run's output
    never pruned, **keep-set never emptied even when `keep` is 0**, and a dump with
    `archive_written` but no `payload_decoded` excluded from the count.
  - Manifest integrity: a size or SHA-256 mismatch invalidates the pair; a torn
    manifest is rejected rather than partially trusted.
  - A zero vector returned by a mocked provider results in a stored memory with a
    NULL embedding and `embedded: false` — **not** an exception out of
    `memory_store` or the REST update.
  - Filename collision: publication of a dump whose final path already exists
    raises rather than overwriting (`link()`, not `rename()`).
  - TOC allowlist: an unexpected TOC entry fails the run, matched by **exact
    identity** rather than prefix, so a `memory_backup` table or an
    `idx_memory_tags_extra` index cannot pass as an expected object.
  - TOC completeness: a dump missing any expected identity — an index, the PK, a
    function, an extension — fails the run. Requiring only `TABLE DATA` would
    accept a structurally incomplete archive.
  - `update_memory` raises when `content` is supplied with `embedding` left
    `UNSET`, and permits tags/source/importance-only updates without it.
  - `create_validated_embedding` degradation per call site: stores and connectors
    fall back to NULL, while `cli/search.py` and `api/main.py:296` fall back to
    text-only search rather than raising.
  - Publication is no-clobber: an existing final path makes `link()` fail rather
    than overwrite; the directory is fsynced after publication.
  - Retention preserves the current run's dump by identity even when its timestamp
    sorts below the newest 14 (simulated clock rollback), and orders by sequence
    number rather than timestamp.
  - Sequence recovery: a missing state file recomputes from filenames; a corrupt one
    does the same; a reserved-but-unused number leaves a gap without erroring.
  - MCP search (`main.py:718`) degrades to text-only on `EmbeddingError` rather than
    raising.
  - `pg_dump` invocation: argument list correct and the password present in the
    child env but **absent from argv**.
  - Payload-decode failure and TOC-mismatch failure both fail the run.
  - CLI mode exclusivity: rejected combinations exit with usage errors.
- Integration test (`@pytest.mark.integration`, real cluster, skipped in CI):
  - The zero-vector `array_fill` comparison actually behaves as intended in
    pgvector — a mocked cursor cannot establish this. This is the only remaining
    integration test; the end-to-end restore rehearsal is gone with deep verify.
- CI: GitHub Actions on push to `mihaics/openbrain`, unit tests only, no service
  containers needed.

### Manual verification (once, recorded in the implementation plan)

- `systemctl --user start openbrain-backup.service`, then confirm the dump and its
  manifest exist with the expected modes, the sequence file advanced, and the
  manifest's row count matches the live table.
- `openbrain backup --status` and `--status --json` in both states: with a fresh
  dump present, and against an empty backup directory.
- A second invocation while the first holds the lock exits non-zero.
- `systemctl --user list-timers` shows the timer scheduled with `Persistent=true`
  taking effect.

**Not performed:** a restore rehearsal. There is no privilege-free target for one
under this decision, so the restore procedure ships as untested documentation.

---

## Component 4 — Documentation and cruft

Leaving stale operational scripts in place while claiming the repo reflects the
machine is worse than the current state, because each one is a plausible-looking
wrong instruction for a future agent.

### Delete

| File | Why |
| --- | --- |
| `docker-compose.yml` | Declares pg16 container, user `postgres`/password `openbrain`; maps host 5433 and would collide with the live cluster |
| `Dockerfile`, `web/Dockerfile`, `.dockerignore` | Supporting cast |
| `scripts/backup.sh` | Port 5432, user `postgres`, plaintext `.sql`, unguarded `find -mtime +7 -delete`, dead S3 hook. Superseded by `openbrain backup` |
| `scripts/healthcheck.sh` | Port 5432, user `postgres`, queries a nonexistent `memories` table |
| `scripts/startup.sh` | `DB_HOST=postgres`, port 5432 — Docker-era assumptions |
| `scripts/setup_db.py` | Creates database and extension, assuming privileges `openbrain` does not have |
| `~/.config/systemd/user/openbrain-dashboard.service` | Orphan unit for the Streamlit UI removed in `3f39c70`. **Copied into `deploy/systemd/attic/` first** — it is untracked, so deleting it is not otherwise recoverable |

`scripts/remember.sh` and `scripts/check_db.py` are kept — reviewed and still
accurate.

### Rewrite

- `README.md`: the two systemd units and their management, PostgreSQL 18 on 5433,
  Ollama `qwen3-embedding:0.6b` at 1024 dims, the six MCP registration paths, and a
  backup/restore runbook. The restore half of that runbook is **documented but
  unexercised** — see Accepted Risks — and must say so where a reader will see it,
  not bury it.
- `.env.example`: port 5433, user `openbrain`, and a note that
  `config/settings.yaml` is authoritative for the embedder.
- `requirements.txt`: reconcile with `pyproject.toml` or delete in favor of it.
- `src/main.py` module docstring: says FastMCP; the server uses the low-level
  `mcp.Server` API.
- `SPEC.md`: mark historical with a pointer to the README. It documents ivfflat
  (schema uses HNSW), port 5432, FastMCP, and a `get_today_memories` function.

---

## Error handling

| Failure | Behavior |
| --- | --- |
| Cluster unreachable | Bounded retry (5 attempts, backoff), then non-zero exit; nothing promoted, nothing pruned |
| `pg_dump` non-zero | `.partial` left for inspection, no manifest, no pruning, non-zero exit |
| Payload decode or TOC mismatch | Archive renamed `.unverified`, manifest records the failure, no pruning, non-zero exit |
| Lock held | Immediate non-zero exit, "another backup is running" |
| Provider returns a zero vector | Treated as an embedding failure inside the handler: memory stored/updated with NULL embedding, `embedded: false`. Never an exception to the caller |
| Manifest size/SHA-256 mismatch | The dump is treated as unverified: excluded from the retention count, never pruned |
| Backup dir unwritable | Non-zero exit before dumping |
| Embedder down during `reembed` | Rows stay NULL after 3 consecutive failures, non-zero exit, next run retries |
| Re-embed fails on update | Embedding explicitly NULLed; FTS-findable; swept later |

## Rollback

Deletions are recoverable from git history, except the untracked orphan unit,
which is copied to `deploy/systemd/attic/` first. New unit files are tracked in
the repo and installed by symlink, so removal is `systemctl --user disable --now
openbrain-backup.timer` plus deleting the symlinks. Code changes are additive
apart from `update_memory`'s sentinel default, which is covered by new tests. No
database-level artifacts are created, so there is nothing to drop.

## Accepted risks

1. **Disk loss loses the database and every dump.** Local-only was chosen
   deliberately. Covers accidental deletion (including a `memory_bulk_delete`
   mistake), corruption, and bad migrations; not drive failure, theft, or
   filesystem loss. Adding a copy step later is small. Recorded so the gap is a
   choice, not an oversight.
2. **The dump is a plaintext-recoverable copy of all memory content** at `0600`
   in `~/backups`. Consistent with `config/settings.yaml` already holding the
   database password in plaintext.
3. **A silently failing timer goes unnoticed until sub-project 3.** Mitigated
   meanwhile by `--status --json`. This is the same failure class as the capture
   gap that motivated the work, which is why the health signal is a named
   deliverable of sub-project 3.
4. **Restorability is never proven, and the restore path will be unexercised.**
   Decided deliberately: no superuser step, so no verification database, so no
   restore rehearsal. Verification proves each archive is structurally sound and
   fully decodable — which catches truncation, bit-rot, and a broken `pg_dump` — but
   nothing exercises `CREATE EXTENSION`, schema creation, index rebuilds, or the
   `vector(1024)` column. If a restore is ever needed, that will be the first time
   those commands have run on this machine. Two consequences worth naming:
   - The runbook's restore section is documentation, not a tested procedure. It
     should be written to say so.
   - `openbrain reembed` becomes the practical recovery path for embedding-level
     damage, since it needs no restore at all.

   Should this ever be worth revisiting, it does **not** require granting anything
   on the live cluster: `initdb` into a temporary directory creates a throwaway
   cluster where this user is superuser, so `CREATE EXTENSION` and a full restore
   both work with zero privileges on the main cluster and no permanent artifacts.
   Recorded as an available option, not a pending task.

## Handoffs to later sub-projects

- **Sub-project 2** must require a fresh dump whose `payload_decoded` succeeded
  immediately before its tag-hygiene backfill, since that mutation rewrites tags
  across every row. `--status --json` exposes the newest verified dump's age for
  exactly this gate. Note this gate is now weaker than intended: it confirms an
  intact archive, not a restorable one, so the backfill should additionally be
  reversible on its own terms rather than relying on the backup as its undo.
- **Sub-project 2** also inherits ownership of embedding-dimension migration and
  scheduled re-embedding. This bundle deliberately ships only the manual
  `reembed` tool; the earlier note that sub-project 2 is "where embedder changes
  live" is made explicit here rather than left implicit, since its stated scope
  (reranking, tag hygiene, access-based ranking) does not otherwise imply it.
- **Sub-project 3** consumes `--status --json`: newest archive age, newest
  *verified* dump age, and the last failure with its reason. Designed as
  machine-readable from the start so no redesign is needed.
- **Dimension changes** cannot invalidate anything here: the dimension is read from
  the live column typmod and recorded in the manifest, never hardcoded and never
  compared against a restored schema.

## Files touched

New: `src/cli/backup.py`, `src/cli/reembed.py`, `deploy/systemd/openbrain-backup.{service,timer}`,
`deploy/systemd/attic/openbrain-dashboard.service`, `.github/workflows/test.yml`, tests.

Permissions changed: `config/settings.yaml` and `.env` from `664` to `0600`.

Modified: `src/cli/__init__.py`, `src/db/queries.py` (sentinel, enforced
content/embedding invariant, central validation), `src/main.py` (update handler,
docstring), `src/api/main.py` (create, update, and search paths),
`src/embedder/__init__.py` (`create_validated_embedding`, `EmbeddingError`,
`embed_batch`), `src/ingestion/importer.py`, `src/cli/store.py`,
`src/cli/search.py`, `src/connectors/gmail.py`, `src/connectors/file_watcher.py`,
`src/connectors/claude_code.py`, `src/connectors/whatsapp.py`,
`src/connectors/telegram.py`, `pyproject.toml`, `README.md`, `.env.example`,
`requirements.txt`, `SPEC.md`, `tests/test_core.py`.

This is a wider blast radius than revision 1 implied — the connectors and the two
search paths are in scope precisely because Goal 2 is false if any call site can
still lose a memory to a bad provider response. The connector edits are each a
one-line swap to the validated helper.

Deleted: `docker-compose.yml`, `Dockerfile`, `web/Dockerfile`, `.dockerignore`,
`scripts/backup.sh`, `scripts/healthcheck.sh`, `scripts/startup.sh`,
`scripts/setup_db.py`, `~/.config/systemd/user/openbrain-dashboard.service`.
