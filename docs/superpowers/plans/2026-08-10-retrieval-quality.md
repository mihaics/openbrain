# OpenBrain Retrieval Quality — Implementation Plan (revision 6)

**Source spec:** `docs/superpowers/specs/2026-08-10-retrieval-quality-design.md`
(**revision 9**, passed adversarial review). The spec owns every design
decision; this plan owns sequencing, file-level mechanics, and verification.

**Goal:** fix hybrid retrieval so both arms can win, make the lexical arm fire
on natural language, blend recency/importance/usage within a bounded window,
and clean the tag vocabulary — plus the two live bugs review uncovered.

## Global constraints

- PostgreSQL 18 on `127.0.0.1:5433`, database/user `openbrain`,
  `rolsuper=false`, `rolcreatedb=false`. Credentials only via
  `src.db.connection.DatabaseConfig`.
- **No new schema objects other than two columns.** `backup.py` fails on any
  TOC entry outside its exact 19-entry allowlist. No ledger table, no
  function, no sequence, no index. After Task 2, `openbrain backup` must still
  verify — that is a gate, not a formality.
- Import style is mixed and stays mixed: `src/main.py` and `src/api/main.py`
  insert `src/` on `sys.path` and import top-level (`from db.queries import`);
  `src/cli/*` and `src/connectors/*` use relative imports. New tests import
  `src.<module>` and **patch at the use site**; MCP/API tests take exception
  and helper symbols from the module under test.
- `src/main.py` is an MCP **stdio** server. Nothing it imports may write to
  stdout; diagnostics go to `sys.stderr`.
- Integration tests (`-m integration`) run **only** against a separate
  `openbrain_test` database and must assert `SELECT current_database()` on the
  live connection first. Never against the production database.
- Commit after every task. No AI attribution in commit messages.

## Baseline and the standing gate

`.venv/bin/python -m pytest -q` → **123 passed, 1 deselected**. Interpreter is
**Python 3.14.4** (the spec's "3.11+" is a floor, not the deployment version).

**Standing gate, applies to every task below:** run the full suite and confirm
no regression *before* committing, and before any migration, service restart,
or live data mutation. A task that changes query, tagging, or mutation paths
and skips this has not been verified, only written.

---

### Task 0 — Provision the integration database (prerequisite, runs AFTER Task 2)

Tasks 4, 5, 7, 9 have `-m integration` steps, and **the `openbrain` role
cannot create a database** — `rolcreatedb` is `f` (verified). Without this
task those steps cannot run at all.

**Ordering — linear, no cycle.** Revision 3 had Task 0 run after Task 2 while
Task 2 migrated a database Task 0 had not yet created. The sequence is:

```
Task 2: implement + unit tests + full suite
      → migrate PRODUCTION → verify openbrain backup → commit
Task 0: provision openbrain_test → run `openbrain migrate` against it
      → add the fixture → commit
```

Task 2 migrates production only. `openbrain_test` is migrated here, by this
task, right after it is created. `schema.sql` uses
`CREATE TABLE IF NOT EXISTS`, which does **not** add columns to an existing
table, so the migrate step is required rather than optional.

**One-time, requires sudo.** The superuser commands use `-p 5433` **without
`-h`**, so they reach the right cluster over the Unix socket under peer
authentication. Adding `-h 127.0.0.1` would force TCP, where `sudo -u postgres`
supplies no credential at all and SCRAM host rules reject the connection —
and omitting `-p` entirely would silently target a different cluster on 5432.

```bash
sudo -u postgres createdb -p 5433 -O openbrain openbrain_test
sudo -u postgres psql -p 5433 -d openbrain_test \
  -c 'CREATE EXTENSION "uuid-ossp"' -c 'CREATE EXTENSION vector'
# operator's own shell, one-off provisioning - not application code, so the
# DatabaseConfig-only rule does not apply here:
read -rsp 'openbrain password: ' PGPASSWORD; export PGPASSWORD
psql -h 127.0.0.1 -p 5433 -U openbrain -d openbrain_test -f src/db/schema.sql
DB_NAME=openbrain_test .venv/bin/openbrain migrate    # adds the two columns
unset PGPASSWORD
```

- [ ] **0.1** Provision as above, then confirm
      `psql ... -d openbrain_test -c '\d memory'` shows both new columns.
- [ ] **0.2** Add the `integration` fixture in `tests/conftest.py`. It must
      handle **both database-module identities**: `src/main.py` and
      `src/api/main.py` load `db.connection` while the CLI and tests load
      `src.db.connection`, and each carries its own `DatabaseConfig._instance`
      and pool. Resetting one and asserting on its connection proves nothing
      about a committed mutation routed through the other. The fixture
      therefore walks `sys.modules` for **every** loaded identity, closes each
      pool and clears each singleton, points `DB_NAME` at `openbrain_test`,
      and asserts `SELECT current_database()` **on the connection the code
      under test actually mutates through**.
- [ ] **0.3** The fixture **fails** — never skips — when `-m integration` was
      explicitly requested and the database is absent, and fails if
      `current_database()` returns the production name. A silent skip would
      let the SQL-validity verification be bypassed, which is the one thing
      mocked tests cannot cover.
- [ ] **0.4** Commit: `test: provision an isolated integration database`

---

### Task 1 — Backup manifest carries database identity

**Why first:** the retag gate (Task 9) requires an identity-bearing manifest.
If this lands after the post-migration backup, the newest verified dump has no
identity and retag refuses to run until the next nightly.

**Files:** modify `src/cli/backup.py`, `tests/test_backup.py`

- [ ] **1.1** Failing test: `take_dump` writes `database: {host, port, name}`
      into the manifest, sourced from `DatabaseConfig`, and never the password.
- [ ] **1.2** Add the field in both the success and `.unverified` manifest
      paths (recovery needs it as much as a clean dump).
- [ ] **1.3** `status_report` surfaces `newest_database_identity`.
- [ ] **1.3b** Make the backup root overridable via `OPENBRAIN_BACKUP_DIR`
      (falling back to `~/backups/openbrain`), and derive the retag lock,
      `.retag-sequence` and undo paths from the same root. Without this, any
      test-database backfill would read production manifests — failing
      identity validation — and would write its lock, sequence and undo files
      into the production retag namespace. **Scope of this task's test:** the
      backup-root resolver only — `src/cli/retag.py` does not exist until
      Task 9, so nothing here can verify the lock, sequence or undo paths.
      Task 9 owns asserting those actually derive from the override.
- [ ] **1.4** Full suite green; run `openbrain backup` for real and confirm
      the new dump's `.json` contains the field and still verifies.
- [ ] **1.5** Commit: `feat(backup): record database identity in the manifest`

---

### Task 2 — Migration, schema validation, and a barrier that is not swallowed

**Files:** create `src/cli/migrate.py`, `tests/test_migrate.py`; modify
`src/db/schema.sql`, `src/db/connection.py`, `src/cli/__init__.py`,
`src/main.py`, `src/api/main.py`

**Interfaces produced:**
`validate_schema() -> None` raising `SchemaMismatchError`;
`migrate_cmd(args) -> int`.

- [ ] **2.1** Failing tests: both columns added; re-running is a no-op; a
      pre-existing `access_count` of the wrong type, nullability, or default
      **fails loudly** rather than passing an existence check; both ALTERs run
      in one transaction; `migrate` itself is exempt from the barrier.
- [ ] **2.2** Implement `migrate_cmd`: two `ADD COLUMN IF NOT EXISTS` in a
      single transaction. **No other schema object.**
- [ ] **2.3** Implement `validate_schema()` reading `information_schema.columns`
      and checking name, type, nullability, default.
- [ ] **2.4** Wire the barrier so it **cannot be swallowed**:
      `init_server()` currently catches everything and sets `_DB_READY=False`;
      the FastAPI lifespan catches `init_db()` failure and still yields with
      `/health` healthy. `SchemaMismatchError` must bypass both — MCP exits
      non-zero; the API records not-ready, and the **shared readiness guard**
      (not `/health` alone) rechecks on any blocked database request once a
      10s cache expires, so migrating clears it without a restart.
- [ ] **2.5** Add the columns to `schema.sql` for fresh installs.
- [ ] **2.6** Register `migrate` in the CLI, exempt from the barrier.
- [ ] **2.7** Barrier tests, not just the validator: MCP **propagates**
      `SchemaMismatchError` and exits non-zero; the API returns 503 from
      `/health` **and** from database-backed endpoints; the shared guard
      clears the state after a successful migration without a restart.
- [ ] **2.8** **Full suite green first**, then run `openbrain migrate` against
      the live database, then **`openbrain backup` and confirm it still
      verifies** (columns must not alter the 19-entry TOC). Migrating before
      the suite passes mutates the schema on unverified code. **Production
      only** — `openbrain_test` does not exist yet and is migrated by Task 0,
      which runs next.
- [ ] **2.9** Commit: `feat(db): add access columns with validated migration`

---

### Task 3 — Ranking configuration

**Files:** create `src/config/ranking.py`, `tests/test_ranking_config.py`;
modify `config/settings.yaml`, `src/main.py`, `src/api/main.py`,
`src/cli/__init__.py`

- [ ] **3.1** Failing tests for the spec's config table: defaults when the
      block is absent; per-field env override; **`w_rec+w_imp+w_acc > 0.10`
      rejected**; negative weight, `half_life_days <= 0`, `access_cap <= 0`
      rejected; rerank enabled without `url`/`model` rejected; the offending
      key is named in the error.
- [ ] **3.2** Implement `RankingConfig` mirroring `DatabaseConfig`'s
      YAML-then-env-per-field pattern. Validation runs at load.
- [ ] **3.3** **Wire it into startup** — `src/main.py`, `src/api/main.py`,
      `src/cli/__init__.py`. Creating the module alone cannot deliver "invalid
      configuration fails at startup"; without this it would first load on the
      first search, which is exactly the behaviour the spec rejects. Test that
      an invalid config fails MCP and API startup, naming the key.
- [ ] **3.4** Full suite. Commit: `feat(config): add validated ranking configuration`

---

### Task 4 — Lexical arm, symmetric fusion, restructured query

The core fix. No data mutation; revertible by deploying the previous commit.

**Files:** modify `src/db/queries.py`, `src/api/main.py`, `src/cli/search.py`,
`src/main.py`; create `tests/test_retrieval.py`

- [ ] **4.1** Failing tests: generated SQL ORs lexemes via
      `replace(plainto_tsquery(...)::text,' & ',' | ')::tsquery` in a single
      leading CTE; both arms use `k=60`; `%`, `_` **and `\`** escaped with
      `ESCAPE '\'`; an empty tsquery disables the **whole** lexical arm
      including ILIKE; every `DESC` carries `NULLS LAST`; `ROW_NUMBER`
      orderings end `, id`; the caller `LIMIT` appears exactly once and last.
- [ ] **4.2** Rewrite the branches to the spec's structural order: arms →
      full outer join → `rrf` → order by `rrf` → `LIMIT :candidate_n`.
      **The current code applies the caller's LIMIT inside the `rrf` CTE;
      that is the bug being removed.** Normalization, secondary blending and
      promotion are **Task 5's** exclusively — this task stops at relevance
      ordering and the final limit, so the two tasks cannot both claim to
      introduce blending.
- [ ] **4.3** `candidate_n = limit` and SQL truncates — **the widening path
      is deliberately not built here.** Widening `candidate_n` without an
      application-side reranker and truncation (Task 10) would let a valid
      enabled configuration over-return for the whole of Tasks 4–9. Widening,
      reranking and truncation land atomically in Task 10.
- [ ] **4.4** Validate `limit >= 1` in MCP, REST, and CLI before computing
      `pool`/`candidate_n` (only MCP clamps today; `LIMIT -1` is a PG error).
- [ ] **4.5** **All four branches are rewritten and tested here**, not just
      hybrid: hybrid, text-only, vector-only, and no-signal, including filter
      parameter ordering — the filter clause is interpolated into both arms
      and its params are passed twice, which is exactly where a restructure
      breaks silently.
- [ ] **4.6** **Live bug fix:** REST `/memories/search` passes
      `importance_min` **and** `importance_max` into `search_memories`.
      `MemorySearch` has no `importance_max` field today (verified), so the
      model gains one. Test: a restrictive minimum returns a full page.
- [ ] **4.7** Integration test (`-m integration`, requires Task 0): the
      tsquery cast and **all four branches** are valid SQL. The
      LIMIT-promotion test belongs to Task 5, which implements blending.
- [ ] **4.8** Full suite; then fixed probe queries with expected ids and
      order, recorded in the test file — "results look sensible" is not a
      verification.
- [ ] **4.9** Commit: `fix(search): let both retrieval arms win`

---

### Task 5 — Blending, clamping, and `--explain`

**Files:** modify `src/db/queries.py`, `src/cli/search.py`, `src/cli/__init__.py`;
modify `tests/test_retrieval.py`

- [ ] **5.1** Failing tests: NULL `created_at` ⇒ recency **0, not 1**
      (`GREATEST(NULL,0)` is `0`); future date ⇒ ≤ 1; NULL/out-of-range
      importance clamped; negative `access_count` clamped; the access ratio is
      **not** integer-divided to 0; an all-zero `rel_raw` set yields score 0,
      not NULL.
- [ ] **5.2** Boundary test: construct rows at a known `rel_norm` gap; assert
      gap > 0.10 ⇒ no flip, gap < 0.10 ⇒ flip possible, with a tolerance
      rather than bit-exact equality.
- [ ] **5.3** Implement blending in SQL per the spec, with `rel_raw` defined
      per branch and `rel_norm` computed once.
- [ ] **5.4** Integration test: a row outside the pre-blend caller limit is
      **actually promoted** into the top N. This lives here because Task 5 is
      where blending exists; asserting it in Task 4 would test nothing.
- [ ] **5.5** `--explain` / `--explain --json` with the per-branch field
      matrix; `degraded: true` on embedding failure. **Ordinary
      `search --json` keeps its current shape**; explain fields never enter
      MCP or REST payloads, and `include_score` still returns one `score`.
- [ ] **5.6** Full suite. Commit: `feat(search): blend recency, importance and usage`

---

### Task 6 — Stop destroying user tags (live bug)

**Files:** modify `src/db/queries.py`, `src/main.py`; create
`tests/test_tag_provenance.py`

- [ ] **6.1** Failing test reproducing the live bug: update a memory's
      `content` through `handle_memory_update` with **no** `tags` argument and
      assert a pre-existing `user` tag survives. It does not today —
      `auto_tag(content, entities, source, user_tags or [])` replaces the set.
- [ ] **6.2** `update_memory_tags` stamps provenance `user` on every
      explicitly submitted tag.
- [ ] **6.3** Content-only updates **merge** recomputed automatic tags with
      retained `user`/unknown ones, **protected provenance winning every
      collision**, under `SELECT ... FOR UPDATE` in one transaction so a
      concurrent explicit tag update cannot be read-then-overwritten.
- [ ] **6.4** Test the collision rule directly: stored `python: user` plus
      recomputed `python: keyword` stays `user`.
- [ ] **6.5** Commit: `fix(tags): stop content updates from deleting user tags`

---

### Task 7 — Access tracking

**Files:** modify `src/main.py`, `src/cli/__init__.py`, `src/db/queries.py`;
create `src/cli/get.py`, `tests/test_access_tracking.py`

- [ ] **7.1** Failing tests: `memory_get` increments; **`memory_search` does
      not**; REST `GET /memories/{id}` does not; a failing update is swallowed
      and the read still returns; the kill switch disables both writes and the
      annotation change.
- [ ] **7.2** `record_access(ids)` **in `src/db/queries.py`**, so the MCP
      handler and the CLI share one implementation rather than duplicating it,
      using `WHERE id = ANY(%s::uuid[])` — the
      cast is required, and because failures are swallowed its absence would
      silently disable tracking forever. Runs after the read transaction
      commits, before the handler returns, in its own transaction.
- [ ] **7.3** Call it from the MCP `memory_get` handler and a new
      `openbrain get ID` CLI command — there is no fetch-by-id CLI command
      today, and instrumenting CLI `search` would violate the no-search rule.
- [ ] **7.4** Flip **both** `readOnlyHint` and `idempotentHint` to `False` for
      `memory_get` only; `memory_search` keeps its read-only annotation.
- [ ] **7.5** Integration test in `openbrain_test`: the counter actually
      increments.
- [ ] **7.6** Commit: `feat(search): track explicit memory access`

---

### Task 8 — Tagger corrections

**Files:** modify `src/extractors/entities.py`, `src/extractors/tagger.py`,
`config/settings.yaml`, `src/setup/__init__.py`; create
`tests/test_tagger_guards.py`

- [ ] **8.1** Failing tests from the spec's two tables: every sufficient alias
      emits without the bare token; every qualified context requires both;
      **negatives — "go build a prototype", "I need to go test it",
      "frontend" alone — emit nothing**; `go1.24` is matched as an alias
      (`\bgo\b` does not match it, verified); canonicalization applies to
      generated tags only, so a user-owned legacy `node` survives.
- [ ] **8.2** Implement the guard at entity extraction; route entity→tag
      conversion through the same canonicalization table as the keyword layer,
      so `node`/`nodejs` cannot both enter.
- [ ] **8.3** Empty `default_tags` in `config/settings.yaml` **and close the
      two paths that reintroduce it** (both verified): `src/setup/__init__.py`
      writes `'default_tags': ['auto']` into generated config, and
      `TagConfig` falls back to `['auto']` when the key is absent. Emptying
      the YAML alone leaves `auto` one fresh setup or one missing key away.
      Tested.
- [ ] **8.4** Full suite, then commit.
- [ ] **8.5** **Then** `systemctl --user restart openbrain-api` and restart
      MCP clients — `TagConfig` and the tagger are process singletons, so a
      live process would re-add `auto` while the backfill removes it.
      `openbrain-web` needs no restart; it does not run the Python tagger.
      Verify readiness, then confirm a freshly stored memory gets no `auto`
      tag **against `openbrain_test`, not production** — the check would
      otherwise leave an unexplained test memory in the corpus with no
      cleanup. Restarting every MCP client is an explicit **blocking operator
      action**: one restarted API process proves nothing about a stale MCP
      server still holding the old singleton, and any such process will
      re-add `auto` during the backfill.

---

### Task 9 — `openbrain retag` with undo

The only destructive task. Everything in spec §3.2 is load-bearing.

**Files:** create `src/cli/retag.py`, `tests/test_retag.py`; modify
`src/cli/__init__.py`

- [ ] **9.1** Failing tests: `user` and unknown-provenance tags survive; a
      provenance-only difference is selected by `--limit`; forward CAS
      conflict is skipped and reported; undo round-trips; a content edit after
      the backfill makes undo refuse without `--force`; a malformed mid-file
      record is rejected before the first write while a **truncated final
      record is discarded and undo still proceeds**; unmarked plans are
      reported indeterminate and **never restored automatically, including
      when the row still matches `after_*`**; a concurrent run is blocked by
      the lock; `--undo` refuses on mismatched database identity.
- [ ] **9.2** Implement: `flock` on `.retag.lock`; backup gate requiring
      `0 <= age < max` **and** matching manifest database identity, exempt for
      `--undo`; undo file `O_CREAT|O_EXCL`, 0600, fsynced, own
      `.retag-sequence`, header with database identity and schema-version
      hash; forward CAS on `id, content, source, tags, tag_sources` using
      `IS NOT DISTINCT FROM` for the nullable columns; **applied marker
      appended and fsynced after commit**; undo CAS on `after_*`, `source`,
      and `md5(content)` evaluated in SQL.
- [ ] **9.3** Explicit mechanics the plan owns, not just the spec: entities
      are **re-extracted from content in memory and never written back**; the
      header and **every plan record are written and fsynced, and the parent
      directory fsynced, before the first mutation**; the whole file —
      record-type uniqueness, `applied`→`plan` references, database identity,
      schema hash — is validated before any write.
- [ ] **9.4** Gate tests: retag refuses with no backup, a stale backup, a
      **future-dated** backup, and a mismatched database identity; `--undo`
      alone is exempt from the age gate.
- [ ] **9.5** **Integration test end to end on `openbrain_test`**, not just
      mocked units. Prerequisites, because forward retag demands a fresh
      verified backup whose manifest identity matches the target database:
      point `OPENBRAIN_BACKUP_DIR` at a temporary directory, run
      `DB_NAME=openbrain_test openbrain backup` there so the manifest carries
      the test identity, and only then invoke retag — so the run never reads a
      production manifest nor touches the production lock, sequence or undo
      files. Then seed disposable rows, run a real backfill, and exercise
      the forward CAS, the committed mutation, the applied-marker protocol and
      the undo CAS against live PostgreSQL — including a CAS conflict and an
      undo round-trip. Without this the destructive production run in 9-OPS
      would be the first time any of that machinery touches a real database.
- [ ] **9.6** Full suite. Commit the implementation — `feat(cli): add retag
      backfill with undo` — **before** any live run.

---

### Task 9-OPS — The backfill rollout (operational, no code)

Separated because revision 1 performed the destructive live run *before*
committing the code that performs it.

- [ ] **9-OPS.1** Confirm Task 8's restart happened and no live process still
      emits `auto`.
- [ ] **9-OPS.2** `openbrain backup`; confirm verified **and** that its
      manifest carries the database identity from Task 1.
- [ ] **9-OPS.3** `openbrain retag --dry-run`; expect `auto` on ~186 rows and
      `go` on ~20–25. **Review the counts before proceeding.**
- [ ] **9-OPS.4** Real run.
- [ ] **9-OPS.5** Verify recoverability *mechanically*, not by eyeballing:
      run `openbrain retag --undo FILE --dry-run`, confirm every mutated row
      has an applied marker and none is reported indeterminate. Then verify
      protected tags **through provenance, not tag names**: for each undo
      record, read `before_tag_sources`, take every tag whose provenance is
      `user` or missing/unknown, and assert it is still present in the live
      row **and still carries protected provenance**. Comparing `before_tags`
      alone cannot tell a protected tag from a removable automatic one, so it
      would pass even if the backfill had stripped exactly what it must keep.
      "Undo file readable" proves nothing.

---

### Task 10 — Reranker seam (disabled)

**Files:** create `src/rerank/__init__.py`, `tests/test_rerank.py`; modify
`src/db/queries.py` call path, `src/cli/search.py` (rerank explain fields)

- [ ] **10.1** Failing tests: bypassed when the query has no text; ordering by
      `relevance_score DESC, original_sql_index ASC` (array order not
      trusted); absent indices keep SQL order and are appended; duplicate,
      out-of-range, NaN or infinite scores reject the response wholesale;
      non-object entries and more results than documents rejected; request
      size caps enforced; `top_n < limit` still fills toward `limit`; final result
      cardinality is **`min(limit, candidate_count)` with no duplication** —
      "exactly `limit`" is impossible when filters or corpus size yield fewer
      candidates, and fill-to-limit applies only when at least `limit`
      candidates exist; unreachable ⇒ unchanged order.
- [ ] **10.1b** Candidate widening (`pool`/`candidate_n` on
      `rerank_applicable`) and application-side truncation land **in this
      task, atomically with the reranker**, plus the `rerank_score` /
      `rerank_order` / `rerank_degraded` explain fields — which means this
      task also modifies `src/cli/search.py`.
- [ ] **10.2** Implement behind `ranking.rerank.enabled` (default false), with
      the `top_n ≤ 200`, 1 MB request and 4 KB per-document caps.
- [ ] **10.3** Commit: `feat(search): add optional reranker seam`

---

### Task 11 — Documentation

**Files:** modify `README.md`, MCP tool descriptions in `src/main.py`

- [ ] **11.1** Document retrieval: hybrid fusion, the blending window,
      `--explain`, and **query input as literal natural language with no
      operator syntax** (`-foo` is not negation and never was).
- [ ] **11.2** Document `openbrain migrate`, `openbrain get`, `openbrain
      retag`, and the `openbrain_test` requirement for integration tests.
- [ ] **11.3** Run every documented command before claiming it works — the
      sub-project 1 lesson — but **destructive examples (`retag`, `retag
      --undo`) run only against `openbrain_test`**. Against production, verify
      with `--help` and `--dry-run` only. A documentation check must not be
      able to rewrite the corpus. Destructive examples also run with
      `OPENBRAIN_BACKUP_DIR` pointed at a temporary directory, for the same
      reason as Task 9.5 — otherwise a documentation check writes into the
      production retag namespace.
- [ ] **11.4** Commit: `docs: describe retrieval behaviour and new commands`

---

## Task order rationale

1 before 2 so the post-migration backup carries identity. 2 before 4–5 because
the ranking SQL references the new columns. 3 before 4 because the query needs
weights. 6 before 9 because the backfill depends on the corrected provenance
semantics. 8 before 9, with a service restart between, or live processes
re-add what the backfill removes. 10 and 11 last.

Tasks 4, 5, 10 mutate no stored data. Tasks 1, 2, 6, 7, 8 change schema,
manifest content, provenance, counters, or what later writes store. Task 9 is
the only one that rewrites existing rows, and it carries the undo file.

## Verification gates

| After | Must hold |
| --- | --- |
| Every code task | Full suite green **before** commit — 123 passed baseline plus that task's new tests |
| Task 2 | `openbrain backup` still verifies (TOC unchanged); production migrated (test DB is Task 0's) |
| Task 0 | `\d memory` on `openbrain_test` shows both columns after its own migrate step; fixture resets **every** loaded db-module identity and fails, not skips, when the database is missing |
| Task 4 | All four branches valid SQL; fixed probe queries match recorded ids and order |
| Task 5 | A row outside the pre-blend limit is demonstrably promoted |
| Task 8 | `auto` unreachable from settings, generated config, and `TagConfig` default; API restarted; **every MCP client restarted**; no-`auto` check run against `openbrain_test` |
| Task 9 | End-to-end integration test on `openbrain_test` passes, run under an isolated `OPENBRAIN_BACKUP_DIR` with its own identity-bearing verified backup; implementation committed **before** any live run |
| Task 9-OPS | Verified backup carrying database identity; dry-run counts reviewed; after the run, every mutated row has an applied marker, none indeterminate, and every `user`/unknown-provenance tag from `before_tag_sources` is still present with protected provenance |
| Task 11 | Every documented command executed; destructive examples only against `openbrain_test` |
