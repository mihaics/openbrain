# OpenBrain Auto-Capture — Implementation Plan (revision 7)

**Source spec:** `docs/superpowers/specs/2026-08-10-auto-capture-design.md`
(**revision 5**, passed). Spec owns design; this plan owns sequencing, file
mechanics and verification.

## Global constraints

- PostgreSQL 18 on `127.0.0.1:5433`, db/user `openbrain`, `rolsuper=false`.
- **Exactly one new schema object**, `capture_state`. Its TOC entries come
  from `pg_restore -l` on a real dump, never predicted.
- `src/db/queries.py` loads under two identities (`db.queries` and
  `src.db.queries`), so new cross-package imports there need the
  dual-spelling try/except already in the file.
- Unit tests mock PostgreSQL and Ollama at the use site and redirect every
  state/config/transcript path to `tmp_path`.
- **Integration tests are a separate gate.** `pyproject.toml` deselects
  `integration` by default, so "full suite green" never runs them. Every task
  with an integration step also runs `pytest -m integration` explicitly, and
  each such test takes the `openbrain_test` fixture asserting
  `current_database()` before touching PostgreSQL or Ollama.
- The hook is on the critical path of every turn: always exits 0, writes
  nothing to stdout or stderr.
- **Standing gate:** full suite green before every commit, and before any
  migration, timer install, hook install or live drain.

## Baseline

`pytest -q` → **255 passed, 12 deselected**; `pytest -m integration` → 12.

---

### Task 1 — `capture_state`, the allowlist, and the backup race

The systemd backup timer runs code from this working tree. Changing
`EXPECTED_TOC_ENTRIES` before production is migrated makes every backup fail;
migrating first makes it fail the other way. **The window must be closed, not
narrowed.**

- [ ] **1.1** `systemctl --user stop openbrain-backup.timer` and confirm no
      backup is mid-run. Nothing below proceeds until it is stopped.
- [ ] **1.2** Define `capture_state` with a **PRIMARY KEY** and a
      `CHECK (id = 1)`. `CHECK` alone does not make a singleton — multiple
      rows with `id = 1` are legal without a key. Columns:

      | Column | Type | Null | Default |
      | --- | --- | --- | --- |
      | `id` | `SMALLINT` PK, `CHECK (id = 1)` | no | `1` |
      | `generation` | `BIGINT` | no | `1` |
      | `previous_generation` | `BIGINT` | yes | — |
      | `committed_work_identity` | `JSONB` | yes | — |
      | `reprocess_counter` | `BIGINT` | no | `0` |
      | `active_reprocess` | `JSONB` | yes | — |
      | `updated_at` | `TIMESTAMP` | no | `now()` |

      `reprocess_counter` and `active_reprocess` are **separate columns on
      purpose** (Task 9.2): incrementing `generation` for a reprocess would
      trip the restore interlock, and reusing `committed_work_identity` would
      destroy the evidence normal crash recovery depends on. Seed exactly one
      row idempotently (`INSERT ... ON CONFLICT (id) DO NOTHING`).
- [ ] **1.3** Extend `validate_schema()` to check the table's columns, types,
      nullability, defaults **and that exactly one row exists** — the startup
      barrier must validate it, not merely tolerate `IF NOT EXISTS`.
- [ ] **1.4** Migrate `openbrain_test`, dump it, and read the **actual** new
      TOC entries from `pg_restore -l | grep -v '^;'`. Update
      `EXPECTED_TOC_ENTRIES` and its `assert len(...) == N` from that output.
- [ ] **1.5** Unit test: a dump containing `capture_state` verifies, and the
      assertion matches the real count.
- [ ] **1.6** Full suite **and** `pytest -m integration`.
- [ ] **1.7** Migrate production, run `openbrain backup`, confirm it verifies.
      **Recovery branches on whether the migration committed**, because
      reverting blindly is worse than failing: if the schema change landed,
      restoring the old allowlist guarantees every backup fails against the
      new schema. So — migration committed → fix the allowlist forward, never
      revert it; migration did not commit → revert the `backup.py` change.
      **In both cases the timer stays stopped until a backup verifies against
      the actual production schema.**
- [ ] **1.8** `systemctl --user start openbrain-backup.timer`; confirm armed.
- [ ] **1.9** Commit.

---

### Task 2 — Configuration, in two forms

**Files:** `src/config/capture.py`, `config/settings.yaml`,
`~/.local/state/openbrain/capture-hook.json`, tests

- [ ] **2.1** Failing tests: defaults with no block; per-field env override;
      `enabled` defaults **false**; allowlist empty by default; non-loopback
      Ollama URL rejected; caps ≤ 0 rejected; `0 <= dedup_threshold <= 1`;
      non-finite rejected; nonempty `model_digest` and `prompt_version`
      required when enabled; strict boolean parsing; the bad key is named.
- [ ] **2.2** **A hook-readable JSON artifact.** `settings.yaml` needs PyYAML,
      which the stdlib-only hook cannot import — so the hook cannot read the
      allowlist from it at all. `capture --sync-hook-config` writes a minimal
      JSON file (enabled flag, allowlist, queue root) atomically
      (temp→fsync→replace→dir fsync), and the hook reads only that. Canonical
      project identity is the **resolved absolute cwd**, matched exactly.

      **The artifact is a cache, never the authority.** `--install-hook` and
      any config change regenerate it, and the **worker independently
      re-checks the current allowlist** before any model call or storage
      write. Otherwise a stale, wider artifact keeps capturing a project that
      was removed, and a missing artifact silently disables the rollout while
      appearing installed. Tested both ways.
- [ ] **2.3** Full suite. Commit.

---

### Task 3 — Transcript reading, boundaries, ordering

**Files:** `src/capture/transcript.py`, tests + fixtures

- [ ] **3.1** Failing tests on a **fixture** transcript: `tool_result`,
      `tool_use`, `thinking`, meta and sidechain excluded; only human and
      assistant **text blocks** survive; turns group by `promptId`; a trailing
      partial line is ignored and its bytes are not counted; no
      minimum-length rule.
- [ ] **3.2** Encode the measured shape: 75 of 85 `user` records were
      `tool_result` — a top-level `type` filter is the bug being avoided.
- [ ] **3.3** **Ordering is by `(session_id, byte_offset)`, not by
      `boundary_id`.** A hash cannot participate in `boundary <= progress`.
      `boundary_id` is the identity; `byte_offset` is the order, with
      `last_record_uuid` cross-checked for consistency and a mismatch treated
      as transcript replacement.
- [ ] **3.4** Offsets computed from **binary** reads so multibyte UTF-8 cannot
      corrupt the watermark. Defined behaviour for truncation, replacement and
      UUID disagreement.
- [ ] **3.5** Overlapping windows for oversized transcripts **return window
      results for aggregation** — they do not each become a boundary (Task 7).
- [ ] **3.6** Full suite. Commit.

---

### Task 4 — Queue and claim protocol

**Files:** `src/capture/queue.py`, tests

- [ ] **4.1** Failing tests: non-UUID `session_id` rejected before becoming a
      filename; path outside configured roots rejected; symlink rejected; dir
      `0700`, files `0600`; temp→fsync→`os.replace`→dir fsync; a second drain
      exits quietly when the `flock` is held; claim is an atomic rename to a
      unique `.processing`; only the claimed file is deleted; stale claims
      recovered by age; retry count and dead-letter explicit.
- [ ] **4.2** **The claim freezes the boundary.** It durably records target
      `byte_offset`, `last_record_uuid`, `last_prompt_id`, the full work
      identity and the generation. Without this, a transcript that grows
      during a retry silently turns the claim into a *different* boundary
      carrying the old retry count. Claim mutations use the same
      temp→fsync→replace→dir-fsync protocol.
- [ ] **4.3** A claim deferred by `not_before` becomes eligible **at that
      time**, without waiting for stale-claim age recovery.
- [ ] **4.4** Full suite. Commit.

---

### Task 5 — The hook

**Files:** `bin/openbrain-capture-hook`, tests

- [ ] **5.1** Failing tests: exits 0 on malformed, empty, oversize stdin,
      unwritable queue dir, missing or malformed hook-config JSON, and unknown
      project; writes nothing to stdout or stderr in any case; skips when the
      project is not allowlisted; respects `stop_hook_active`.
- [ ] **5.2** Implement **stdlib only** — no `src` imports, no PyYAML, no
      database, no model, no network. Reads the Task 2.2 JSON artifact.
      Bounded stdin. Every exception suppressed.
- [ ] **5.3** Assert a **generous upper bound** on wall time over 20 runs and
      report median and max separately. "Record it in the output" is not a
      gate.
- [ ] **5.4** Full suite. Commit.

---

### Task 6 — Extraction, with typed outcomes

**Files:** `src/capture/extract.py`, tests

- [ ] **6.1** **Typed results, not "yields nothing".** `success(facts)` (which
      includes the ordinary empty list), `content_failure` (malformed or
      over-cap), `infrastructure_failure` (timeout, unreachable, missing
      model, digest mismatch), `capacity_failure`. Task 8 treats these
      differently, and conflating an outage with "no durable facts" would
      advance progress over lost work or dead-letter ordinary quiet turns.
- [ ] **6.2** Failing tests, each encoding a failure found by running it:
      a hijack transcript is **not answered**; `think: false` is sent and a
      payload arriving in `thinking` is still read; schema-constrained output;
      malformed → `content_failure`; timeout/missing model → `infrastructure_failure`;
      non-loopback endpoint refused.
- [ ] **6.3** **Evidence validation is scoped to cited records.** The schema
      requires `record_ids`; every id must belong to a retained block, and the
      evidence quote must occur **within those cited records** — not merely
      somewhere in the transcript, which would let unrelated verbatim text
      legitimise an invented fact. Evidence is validated against the **exact
      redacted text shown to the model**, then discarded.
- [ ] **6.4** Secrets: known credential formats **and high-entropy strings**
      redacted before the call; candidates re-scanned after; an unsafe
      candidate is **discarded, not redacted**. Tests prove no secret reaches
      Ollama, the embedder, storage, errors, `--status` or journald.
- [ ] **6.5** Delimiters framed with a **per-request nonce**, so a transcript
      containing the delimiter cannot break out.
- [ ] **6.6** Health check per drain **resolves and verifies the exact model
      digest**, not merely that the name exists — otherwise a retagged model
      changes behaviour while metadata records the pinned digest.
- [ ] **6.7** Full suite **and** `pytest -m integration` (live Ollama).
      Commit.

---

### Task 7 — One transaction: dedup, insert, and the generation transition

**Files:** `src/db/queries.py`, `src/capture/store.py`, tests

- [ ] **7.1** **The generation transition happens in the same transaction as
      the rows.** Updating `capture_state` afterwards from the worker would
      recreate exactly the crash window the interlock exists to close. One
      transaction: lock and read the singleton, validate the expected
      generation, run dedup and `source_id` checks, insert the whole batch,
      then write `{previous_generation, generation, committed_work_identity}`.
- [ ] **7.2** **Per-fact deterministic `source_id`**, defined here:
      `sha256(boundary_id | prompt_version | model_digest |
      reprocess_generation | normalized_fact_text)`. Without a per-fact
      component, three facts collapse under one id; without the versioned
      components, a reprocess collides with the original.
- [ ] **7.3** **Window aggregation.** All window results are aggregated,
      validated and deduplicated, the per-boundary cap enforced **globally**,
      every embedding generated and validated, and then exactly **one**
      boundary transaction runs. Per-window transactions could triple the cap
      and advance progress after only some windows.
- [ ] **7.4** Failing tests: normalized exact-hash catches duplicates
      **including rows with NULL embeddings**; within-batch duplicates
      collapse; the similarity boundary is tested at exactly `>= 0.92`;
      comparison is exhaustive, not HNSW; if any embedding fails, **nothing**
      is inserted and the claim is retained; `source_id` checked inside the
      transaction, not via `ON CONFLICT`, which has no constraint to act on.
- [ ] **7.5** Rows store `tags=[]`, `tag_sources={}`, `entities={}`, fixed
      importance, `source="claude-code-auto"`, and metadata carrying the
      **canonical work identity** — boundary, prompt version, model digest,
      **and `reprocess_generation`** — plus session id and project identifier,
      never the absolute transcript path. Task 9's recovery queries match that
      complete identity.
- [ ] **7.6** Integration test: rows **and** `capture_state` commit and roll
      back together.
- [ ] **7.7** Full suite **and** `pytest -m integration`. Commit.

---

### Task 8 — The worker

**Files:** `src/capture/worker.py`, `src/cli/capture.py`, tests

- [ ] **8.1** One test per crash window: before commit → retry, nothing
      written; after commit before progress → boundary found in the database,
      progress advanced **without invoking the model**; after progress before
      deletion → stale claim deleted without processing; zero facts →
      progress fsynced before deletion.
- [ ] **8.2** Generation interlock — **one global checkpoint, not a
      per-session comparison.** Revision 4 compared each session's progress
      generation against `capture_state`, which breaks in ordinary operation:
      sessions A and B both sit at generation 1, A commits and advances the
      global generation to 2, and B — still at 1, with the latest committed
      identity belonging to A — is refused forever. After the first committing
      session every other session would be permanently blocked.

      So the filesystem keeps **a single global generation checkpoint**
      alongside the per-session boundary progress, and the comparison is
      between that checkpoint and `capture_state.generation`, with explicit
      rebase rules:

      | Situation | Action |
      | --- | --- |
      | DB generation ahead, and the gap is explained by committed work identities | **Rebase** the checkpoint and any lagging claim generations forward, leaving their boundaries untouched |
      | DB ahead **and** the pending work identity is itself committed | Advance that boundary **without invoking the model** |
      | Filesystem checkpoint ahead of the database | **Refuse** — a database restore is the only way this happens |
      | Equal | Normal operation |

      Tested with two interleaved sessions, a stale claim spanning an
      unrelated session's commit, and restore detection **after** a rebase has
      occurred — the case a naive equality check gets wrong in both
      directions.
- [ ] **8.2b** **The checkpoint is a durable artifact and joins the crash
      protocol** — introducing it without that is how the last defect got in.
      It lives at `<state-root>/generation-checkpoint.json`, written
      atomically (temp→fsync→replace→dir fsync) **under the drain lock**, and
      the write order extends §6's:

      **DB commit → checkpoint → per-session boundary progress → delete claim.**

      | Crash window | Recovery |
      | --- | --- |
      | After commit, before checkpoint | Checkpoint behind DB with the gap explained by a committed identity → rebase forward |
      | After checkpoint, before boundary progress | Checkpoint and DB agree, so the generation check passes; the **committed-boundary lookup** still advances that boundary without invoking the model |
      | After boundary progress, before claim deletion | Stale claim deleted without processing |

      A **missing or corrupt checkpoint refuses to drain, with no exception.**
      Revision 6 allowed lazy initialization on a "provably pristine" install,
      which is not provable: restoring a pre-auto-capture database and
      re-running `migrate` produces exactly `generation = 1` with zero
      `claude-code-auto` rows, while stale per-session progress may still sit
      on disk. The worker would initialize the checkpoint, trust those future
      watermarks, and silently skip everything the restore lost.

      Initialization is therefore an **explicit command**, run under the drain
      lock before the timer or hook is enabled, and it refuses unless **both**
      sides are clean:

      - **Filesystem:** no progress, no `.processing` or deferred claims, no
        retry counters, no dead letters, no reprocess state, no reconcile
        intent.
      - **Database:** `generation = 1`, `previous_generation` and
        `committed_work_identity` null, `reprocess_counter = 0`,
        `active_reprocess` null, and zero `claude-code-auto` rows.

      Anything else refuses and directs the operator to `capture --reconcile`.
- [ ] **8.2c** Tests: crashes on **both sides** of the checkpoint write, and a
      drain refused when the checkpoint is ahead of the database.
- [ ] **8.3** Ceiling, with the actual values rather than a promise to state
      them. The day is **UTC by the database clock** — the laptop runs
      Europe/Bucharest, which observes DST, so a local day boundary would
      duplicate or skip an hour of allowance twice a year. Count:

      ```sql
      SELECT count(*) FROM memory
       WHERE source = 'claude-code-auto'
         AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
      ```

      Next reset is `date_trunc('day', now() AT TIME ZONE 'UTC') + interval
      '1 day'`.

      **This only works with an explicit invariant, because `memory.created_at`
      is `timestamp without time zone DEFAULT now()`** — it stores the
      connection's wall clock, so the same instant lands as `18:26` under
      Europe/Bucharest and `15:26` under UTC (verified). Comparing those values
      against a UTC expression would be wrong by the offset. The invariant:
      **every `claude-code-auto` row is inserted with the session timezone
      forced to UTC** (`SET LOCAL TIME ZONE 'UTC'` inside the capture
      transaction), and the counting query runs under the same. Existing rows
      from other sources are unaffected because the count filters on `source`.
      Tested from a session running `Europe/Bucharest`, including across a DST
      transition. The kill switch and an exhausted ceiling are checked **before**
      claiming or invoking Ollama, and capacity is **rechecked inside the
      transaction** against the actual post-dedup row count, since dedup can
      only shrink the batch and the pre-check used an estimate. A boundary is never partially inserted; over
      capacity retains the claim without advancing progress or consuming a
      retry and records a durable `not_before`.
- [ ] **8.4** Retries keyed on Task 6's typed outcomes: `infrastructure_failure`
      backs off globally **without** consuming content retries;
      `content_failure` gets one retry then dead-letters; a new boundary never
      inherits an old count.
- [ ] **8.4b** **Infrastructure failures outside extraction are classified the
      same way** — PostgreSQL unavailable, embedding generation failure,
      transactional store failure, and `capture_state` read failure all route
      to global backoff, retain the claim safely, and consume no content
      retry. Only the model's own output can be "bad content"; everything else
      is an outage, and treating an outage as content would dead-letter real
      work during a restart.
- [ ] **8.5** `capture --status`: every counter's persistence and reset scope
      defined; `last_error` sanitized through an **allowlist** format, never
      free text.
- [ ] **8.6** Full suite **and** `pytest -m integration`. Commit.

---

### Task 9 — Dry-run, scan-recent, reprocess, and restore recovery

**Files:** `src/cli/capture.py`, tests

- [ ] **9.1** `--scan-recent --dry-run` reads transcripts with no queue entry;
      claims nothing, writes nothing, changes no state, touches no counters;
      nearest-memory content hidden by default.
- [ ] **9.2** `--reprocess`: the generation is allocated by incrementing
      `capture_state.reprocess_counter` and recording `active_reprocess`
      (identity plus state) **in one committed transaction before any work**;
      a resumed invocation finds `active_reprocess` and reuses it rather than
      forking a new generation, and clears it only when the run completes. Reprocessing passes through
      the same kill switch, ceiling, retry, transaction and generation
      interlock as normal work.
- [ ] **9.3** **`capture --reconcile` is an implemented, tested command**, not
      a documented procedure — and it cannot be atomic, since it updates one
      database generation and many progress files. So it is an **idempotent
      intent protocol**:

      1. Preconditions: the drain lock is held and the timer is stopped;
         refuse otherwise.
      2. Validate the database identity matches the live configuration.
      3. Write a durable `reconcile-intent` recording the target generation.
      4. Update the database generation, then rewrite progress files from
         versioned row metadata — **database first, files second**, so a crash
         leaves files behind rather than ahead.
      5. **Repair every other state class in the same pass**, not just
         progress: `.processing` claims, deferred (`not_before`) claims,
         retry counters, dead-lettered entries and reprocess claims can all
         carry generations or work identities that no longer exist in the
         restored database. Each class is explicitly rebuilt, invalidated or
         requeued, and each is tested. Rewriting progress alone leaves the
         worker permanently blocked or deletes work it should have redone.
         Tested specifically with a checkpoint **ahead** of the restored
         database.
      6. **Set the global generation checkpoint to the intent's target
         generation** — before clearing the intent. Without this, restoring a
         database at generation 5 while the checkpoint sits at 10 lets
         `--reconcile` finish and clear its intent while every subsequent
         drain still refuses, because the filesystem remains ahead. Reconcile
         would appear to succeed and fix nothing.
      7. Clear the intent last.

      The intent lives at `<state-root>/reconcile-intent.json`, written with
      the same temp→fsync→replace→dir-fsync protocol as every other durable
      file here.

      Recovery is **not** "on startup", which would contradict the
      timer-stopped precondition the moment the timer started a worker.
      Instead: **while an intent exists, an ordinary `--drain` refuses and
      exits non-zero**, and only `capture --reconcile` — holding the drain
      lock — resumes it, repeating from step 4. Repetition is safe because
      rebuilding progress from committed rows is idempotent. Tested for a
      crash **before, during and after** the progress rewrite, and for a drain
      attempted while an intent exists — a single happy-path restore test
      proves nothing about the windows that matter.
- [ ] **9.4** Full suite **and** `pytest -m integration`. Commit.

---

### Task 10 — Deployment surfaces

**Files:** `deploy/systemd/openbrain-capture.{service,timer}`,
`src/cli/capture.py`, tests

- [ ] **10.1** Timer every 15 minutes with `Nice` and `MemoryMax`, installed
      by tracked symlink, `daemon-reload`, `enable --now`, `Persistent`,
      non-overlapping, with a service timeout. Noted: `MemoryMax` bounds the
      worker, **not** the separately running Ollama daemon.
- [ ] **10.2** `--install-hook` / `--uninstall-hook` take an **explicit
      settings-file selector**, since the rollout installs into one persona.
      Preserve unknown keys and existing hooks, add exactly one identifiable
      entry, validate the resulting JSON, atomic replacement preserving mode,
      uninstall removes only that entry.
- [ ] **10.3** Tests assert the **installed contract**: absolute executable
      path, no shell, correct `args`, executable mode, `"async": true`, short
      timeout — against a fixture settings file with unrelated hooks present.
- [ ] **10.4** Full suite. Commit.

---

### Task 11-OPS — Evidence before enabling (no code)

- [ ] **11.1** `capture --scan-recent --dry-run` over recent real transcripts.
- [ ] **11.2** Labelled corpus with a **minimum sample size**, a stated
      precision formula, an explicit target and false-positive budget, and a
      defined bucket for candidates that are neither clear keep nor clear
      discard.
- [ ] **11.3** Miss the target → tune prompt or caps and repeat. Nothing is
      enabled on impression.
- [ ] **11.4** Install the timer with the hook uninstalled; confirm the queue
      stays empty.
- [ ] **11.5** Install the hook for one persona, one allowlisted project, for
      a **stated observation duration**.
- [ ] **11.6** Review what landed; widen only if precision holds.

---

### Task 12 — Documentation

- [ ] **12.1** README: what is captured and what is not, the allowlist, the
      kill switch, `capture --status`, and that bulk removal covers the **live
      database only** — a leaked credential must be rotated regardless.
- [ ] **12.2** Document the restore interlock as the `capture --reconcile`
      command from 9.3, not as prose.
- [ ] **12.3** Run every documented command; destructive ones only against
      `openbrain_test`.
- [ ] **12.4** Commit.

---

## Order rationale

1 first and alone: it is the only task touching shipped backup verification,
and it brackets the change by stopping the backup timer. 2–4 are
infrastructure with no model and no writes. 5 depends on 2.2's JSON artifact.
6 depends on 2 and 3. **7 owns the transaction, including the generation
transition**, so 8 can be defined in terms of it. 9 adds recovery. 10 is
deployment. 11-OPS is the only task that enables anything, gated on
measurement. 12 last.

## Verification gates

| After | Must hold |
| --- | --- |
| Every code task | Full suite green before commit; `pytest -m integration` for tasks that have integration steps |
| Task 1 | Backup timer stopped across the change; production migrated; `openbrain backup` verifies; timer restarted |
| Task 5 | Exits 0 on every malformed input; asserted wall-time bound |
| Task 6 | Hijack transcript not answered; evidence validated inside cited records; no secret reaches any sink |
| Task 7 | Rows and `capture_state` commit and roll back together |
| Task 8 | Every crash window tested; ceiling derived from committed rows |
| Task 9 | `--reconcile` restores drainable state after a simulated restore |
| Task 11-OPS | Labelled precision meets target **before** the hook is installed anywhere |
