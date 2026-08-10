# OpenBrain Retrieval Quality — Design (revision 3)

Scope: sub-project 2 of 4. Sub-project 1 (reliability bundle) shipped
2026-08-10. Sub-projects 3 (auto-capture) and 4 (transcript-search MCP)
follow and are out of scope.

**Goal in one line:** make `memory_search` return the right memory, and make
the tag vocabulary mean something.

Revision history. R1 → R2: the weight invariant was arithmetically false, and
blending ran *after* the caller's LIMIT so it could promote nothing. R2 → R3:
NULL `created_at` scored as maximally recent, the rerank/LIMIT rule
contradicted itself, and the "no user tag destroyed" guarantee was still
false. Each was verified against the live database before being accepted.

## Measured current state (2026-08-10, live cluster)

| Fact | Value |
| --- | --- |
| Memories | 210, all with embeddings |
| Tags per memory | 11.3 average |
| `auto` tag | 187/210 rows (89%), 186 by `default` provenance |
| `go` tag | 47 rows; only 27 contain "go"/"golang" at all; 22 by `entity` |
| Provenance totals | user 1133, keyword 474, entity 287, default 186, source 179, pattern 120 |

### Defect 1 — vector search cannot outrank keyword search

Fusion uses asymmetric constants, `1.0/(1 + rank_fts) + 1.0/(60 + rank_vec)`:
lexical rank 1 = 0.5000, lexical rank 20 = 0.0476, **vector rank 1 = 0.0164**.
The best semantic match scores below the twentieth-best lexical match.

### Defect 2 — the lexical arm does nothing on natural-language queries

`plainto_tsquery` ANDs every term. Measured: `plainto_tsquery('what should I
do when embeddings fail to generate')` → 0 rows; `websearch_to_tsquery` (same)
→ 0; `to_tsquery('embeddings | fail | generate')` → 71. Two probe searches
returned exactly 0.0164 for every hit — pure vector contribution.

### Defect 3 — ranking ignores recency, importance, and usage

### Defect 4 — the tag vocabulary is mostly noise

`tech_keywords` holds bare `'go'`, `'rest'`, `'api'`, `'ui'`, `'node'`,
matched `\bgo\b` on lowercased text, so the English verb "go" tags a memory as
the Go language.

## Goals

1. Both arms can reach the top; neither is structurally capped.
2. The lexical arm fires on natural-language queries.
3. Recency, importance, and usage adjust ordering by a bounded, tested amount.
4. Meaningful tag vocabulary, with a **precisely stated** guarantee about
   which tags are protected (§5) and every mutation reversible.
5. Verifiable: tests assert behaviour and boundaries, plus an explain mode.

## Non-goals

- No MCP or REST *signature* changes. Two annotations change (§2).
- No embedder or dimension change. No relevance benchmark harness.
- No auto-capture, no transcript search.

## Component 1 — Ranking

### 1.1 Lexical arm

```sql
replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery
```

Computed **once** in a leading CTE. Verified against the live cluster:

| Input | `plainto_tsquery` | After rewrite |
| --- | --- | --- |
| `embeddings fail to generate` | `'embed' & 'fail' & 'generat'` | OR form → 71 rows |
| `what should I do` | *(empty)* | empty tsquery casts fine, matches 0 |
| `x; drop table memory --` | `'x' & 'drop' & 'tabl' & 'memori'` | fully lexemized |
| `foo -bar` | `'foo' & 'bar'` | `'foo' \| 'bar'` |

**Operator semantics.** `plainto_tsquery` discards quotes, `-`, `!`, `&`, `|`,
`<->` *before* the rewrite, so `-bar` is already positive today; OR-ing
amplifies it. Query input is therefore **defined as literal natural language
with no operator syntax**, documented in the MCP tool description and README.
AST-aware operator support is out of scope.

`ts_rank_cd` ranks within the arm. Measured spread on the live corpus for the
OR query above: 0.1000–0.5000 across 71 rows.

**ILIKE fallback, fixed:** escape `\`, `%`, `_` and use `ESCAPE '\'` — today a
query of `%` is a match-all pattern. When the normalized tsquery is empty the
**entire** lexical arm including ILIKE is skipped, because `ts_rank_cd` would
be NULL and `DESC` is `NULLS FIRST`.

### 1.2 Symmetric fusion

```
rrf = COALESCE(1.0/(60 + rank_fts), 0) + COALESCE(1.0/(60 + rank_vec), 0)
```

Removes the structural cap. It does **not** make the stronger match win — RRF
is rank-based and discards magnitude, so a weak rank-1 keyword match ties a
strong rank-1 semantic match. Accepted; raw magnitudes appear in `--explain`.

All `ROW_NUMBER()` orderings end with `, id`. Every `DESC` ordering in this
design specifies `NULLS LAST`, because PostgreSQL defaults `DESC` to
`NULLS FIRST` — without it a NULL `importance` or `created_at` sorts to the
top of the no-signal branch and of every score tie.

### 1.3 Signal blending

Uniform definition across branches: each branch produces `rel_raw`, and

```
rel_norm = rel_raw / NULLIF(MAX(rel_raw) OVER (), 0)                  -- 0..1
recency  = CASE WHEN created_at IS NULL THEN 0
                ELSE exp(-ln(2) * GREATEST(age_days, 0) / half_life) END
imp      = LEAST(GREATEST(COALESCE(importance, 0.5), 0), 1)
acc      = LEAST(GREATEST(COALESCE(access_count, 0), 0), cap)::double precision / cap

score    = rel_norm + w_rec*recency + w_imp*imp + w_acc*acc
```

The explicit `CASE` on NULL `created_at` is not defensive noise. **Verified:
`GREATEST(NULL, 0)` returns `0` in PostgreSQL**, so revision 2's formula gave
an undated memory `exp(0) = 1.0` — maximally recent. `acc` is clamped on both
sides for the same class of reason. The `::double precision` cast is required
because `LEAST(7,10)/10` evaluates to `0` (verified).

| Parameter | Default |
| --- | --- |
| `w_rec` / `w_imp` / `w_acc` | 0.05 / 0.03 / 0.02 |
| `half_life_days` | 180 |
| `access_cap` | 10 |

**Enforced invariant, not merely a default.** Config validation rejects
negative weights and any configuration where
`w_rec + w_imp + w_acc > 0.10`; the bound is a property of the system, not of
the shipped values. Given that bound, a relevance gap **strictly greater than
0.10** cannot be overcome. At a gap of exactly 0.10 the best case is a tie,
resolved by the `created_at DESC NULLS LAST, id` tie-breaker — so the tested
property is "gap > 0.10 ⇒ no flip; gap < 0.10 ⇒ flip possible", with equality
called out as a tie rather than glossed.

### 1.4 Query structure

Revision 1 blended outside the `rrf` CTE, which already applies the caller's
`LIMIT` — blending there can reorder only rows already chosen. Mandatory
order:

```
arm CTEs (each LIMIT pool)
  → full outer join                     -- up to 2*pool candidates
  → rrf
  → rel_norm via MAX(rel_raw) OVER ()   -- over the full union
  → blending
  → ORDER BY score DESC NULLS LAST, created_at DESC NULLS LAST, id
  → LIMIT :candidate_n
```

`pool = max(limit*4, 20)`. The union can hold up to `2*pool` rows, so
"the pool" is defined as **the ordered union**, and `candidate_n` is:

| Reranking | `candidate_n` | Who applies the caller's limit |
| --- | --- | --- |
| disabled | `limit` | SQL, exactly once |
| enabled | `max(limit, top_n)` | the application, after reranking |

This resolves revision 2's contradiction, where SQL applied the final limit
*and* the reranker was promised the pre-limit pool. Exactly one component
truncates, and which one depends on a single flag.

`rel_norm` depends on pool membership, so a memory can score differently at
`limit=5` and `limit=50`. Documented, with raw `rrf` shown in `--explain`.

**Empirically validated end to end** against the live cluster before writing
this revision. For the query "what should I do when embeddings fail to
generate":

| rank_fts | rank_vec | rel_norm | final |
| --- | --- | --- | --- |
| 11 | 2 | 1.000 | 1.0679 |
| — | 1 | 0.543 | 0.6166 |
| 3 | — | 0.525 | 0.5993 |
| 1 | — | 0.543 | 0.5992 |

A memory ranked 11th lexically and 2nd semantically now tops the result —
impossible under the shipped formula. Rows 3–5 show the near-tie reordering
inside the 0.10 window, exactly as specified.

### 1.5 Branches

| Branch | `rel_raw` | Blended |
| --- | --- | --- |
| hybrid | RRF over both arms | yes |
| text-only | `ts_rank_cd` | yes |
| vector-only | `1 - cosine_distance`, clamped 0..1 | yes |
| no signals | none | **no** — ordering is `importance DESC NULLS LAST, created_at DESC NULLS LAST, id`, using the clamped importance expression |

`rel_norm` is always `rel_raw / MAX(rel_raw) OVER ()`, so there is exactly one
definition and no branch normalizes twice.

### 1.6 Explain mode

`openbrain search --explain` / `--explain --json`. Fields per branch, where
`null` means present-but-null (never silently omitted):

| Field | hybrid | text-only | vector-only | no signals |
| --- | --- | --- | --- | --- |
| `rrf` | value | null | null | null |
| `ts_rank_cd` | value or null | value | null | null |
| `cosine_similarity` | value or null | null | value | null |
| `rel_norm`, `recency`, `importance`, `access`, `final` | value | value | value | `final` only |
| `branch`, `degraded` | always | always | always | always |
| `rerank_score`, `rerank_order`, `rerank_degraded` | present when reranking enabled | | | |

`degraded: true` marks an embedding failure that forced text-only. When
reranking reorders results, `final` alone no longer explains the order, so the
reranker's score and pre-rerank position are included — otherwise `--explain`
would misdescribe the output.

**Ordinary `openbrain search --json` keeps its existing shape.** Explain
fields live behind the `--explain` flag on a separate DTO, and never appear in
MCP or REST payloads; `include_score` still returns exactly one `score`.

## Component 2 — Access tracking

```sql
ALTER TABLE memory ADD COLUMN IF NOT EXISTS access_count INT NOT NULL DEFAULT 0;
ALTER TABLE memory ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMP;
```

**Counted on an explicit fetch-by-id only — never on search.** `memory_search`
and `memory_get` both carry
`ToolAnnotations(readOnlyHint=True, idempotentHint=True)`. A counter feeding
ranking is an observable mutation, so for `memory_get` **both** hints flip:
`readOnlyHint=False` and `idempotentHint=False` — repeated calls change state,
so revision 2 flipping only the first was incomplete. `memory_search` stays
genuinely read-only, which matters because hosts auto-approve on that hint.

**Instrumentation boundary, chosen and stated:** the counter is incremented by
an explicit `record_access(ids)` call in the **MCP `memory_get` handler and
the CLI**, never inside `get_memory_by_id`. Instrumenting the query function
would make REST `GET /memories/{id}` state-changing, which violates HTTP
semantics and would let web-UI prefetch inflate counts. Consequence, stated
plainly: REST reads do not contribute to the signal.

```sql
UPDATE memory SET access_count = access_count + 1, last_accessed_at = NOW()
 WHERE id = ANY(%s::uuid[])
```

The `::uuid[]` cast is required — a Python list of `str` adapts as `text[]`
and raises `operator does not exist: uuid = text`. Because the call swallows
exceptions, without the cast tracking would silently never work, so an
integration test asserts a real increment.

Runs after the read transaction commits and before the handler returns, in its
own transaction, `try/except Exception` logging to stderr. A crash between
commit and update loses one increment — the correct trade.

**Kill switch:** `ranking.access_tracking.enabled` (default `true`). If a host
reacts badly to the annotation change, setting it `false` restores the
original annotations and stops the writes without a code change.

## Component 3 — Tag hygiene

### 3.1 Forward-only corrections

- Remove `auto` from `default_tags` (list becomes empty).
- Guards apply only to terms actually in `tech_keywords`: `go`, `rest`, `api`,
  `ui`, `ux`, `node`, `java`. (`head`/`lead` were listed in revision 1 but
  live in the unused `people_roles` set.)
- **Two distinct trigger kinds**, because revision 2 conflated them:

  | Kind | Rule | Examples |
  | --- | --- | --- |
  | Sufficient alias | present ⇒ emit tag, bare token not required | `golang`, `goroutine`, `go.mod`, `gofmt`, `nodejs`, `node.js`, `npm`, `node_modules`, `restful`, `jvm`, `maven`, `gradle` |
  | Qualified context | bare token **and** context phrase both required | `go` + `go1.`; `rest` + `rest api`; `api` + (`api key`\|`api endpoint`\|`graphql api`); `ui` + (`ui component`\|`web ui`\|`ui test`); `ux` + `ux design`; `java` + (`java 8`…`java 25`\|`spring boot`) |

  Revision 2 listed `frontend` as context for `ui`, which would emit `ui` for
  text never containing "ui", and `go build`/`go test` for `go`, which fire on
  "go build a prototype" and "I need to go test it". Both are removed, and
  both appear as **negative test cases** so the original defect cannot return.

- Emission is canonical: `node` and its aliases emit `nodejs` only. The guard
  runs at entity extraction, and entity→tag conversion routes through the same
  canonicalization table, so a term cannot enter under two spellings.
- **Restart barrier.** `TagConfig` and the tagger are process singletons, so
  editing `settings.yaml` does not stop a running MCP or API process from
  emitting `auto`. Services must be restarted *before* the backfill, or a live
  process re-adds what the backfill removes.

### 3.2 Backfill — `openbrain retag`

```
openbrain retag [--dry-run] [--limit N] [--max-backup-age HOURS]
openbrain retag --undo FILE [--force] [--dry-run]
```

Removes only tags that the corrected tagger no longer produces **and** whose
provenance is non-`user`. Unknown or missing provenance fails closed. The
precise guarantee, and its one hole, are in §5.

**Entity policy, previously undefined:** retag **re-extracts entities from
`content` in memory** for the recomputation and **does not write them back**.
Trusting the stored `entities` JSON would reproduce the very false positives
being removed; writing entities would pull them into the CAS and undo record.

Safety, in order:

1. **Single-run lock.** `flock` on `~/backups/openbrain/.retag.lock`, the
   pattern `reembed` already uses. Distinct undo sequences do not serialize
   anything, so without this two runs can interleave through CAS and produce
   partial, interdependent undo files.
2. **Fresh-backup gate.** `status_report(backup_dir)` — it takes an argument.
   Require `0 <= newest_verified_age_seconds < max_backup_age` (default 24h);
   the lower bound rejects a future timestamp. **Additionally the manifest
   must identify this database:** `backup.py` records row count, dimension,
   extensions and versions but no host/port/dbname, so a reused backup
   directory could gate a retag of database B on a dump of database A. This
   sub-project adds `database: {host, port, name}` to the manifest and the
   gate compares it against the live `DatabaseConfig`. That changes manifest
   *content*, not the TOC, so the 19-entry allowlist is unaffected.
   **The gate does not apply to `--undo`**, which must work in an emergency.
3. **Undo file written and fsynced before any mutation.**
   `~/backups/openbrain/tag-backfill-<seq>.jsonl`, `O_CREAT|O_EXCL`, 0600,
   file and parent directory fsynced, using its own `.retag-sequence` — never
   the backup sequence, which would consume backup numbers or race a running
   backup. Header record: `{schema_version, database, created_at}`, where
   `schema_version` is an **application constant plus a hash of the expected
   column signature** read from `information_schema` (there is no ledger table
   by design — see §4). A mismatch blocks `--undo` unless `--force`.
   Per row: `{id, before_tags, before_tag_sources, after_tags,
   after_tag_sources, content_hash, source, at}`.
4. **Forward CAS on the full recomputation input:** `id`, `content`, `source`,
   `tags`, `tag_sources` as read. Zero rows affected → skipped and reported.
5. **Undo is also CAS**, against `after_tags`, `after_tag_sources`, **and
   `content_hash`/`source`**. Revision 2 guarded only the tag fields, so an
   edit that changed content while leaving tags identical would have had
   old-content tags restored onto it. Conflicts are skipped and reported
   unless `--force`. The whole file is validated and duplicate ids rejected
   before the first write.
6. **`--limit` selects rows whose recomputed `tags` *or* `tag_sources`
   differ**, ordered `created_at ASC, id ASC`. Comparing tags alone would
   never select a row whose provenance map is wrong but whose tag list
   matches, and the run would never converge.
7. `--undo --dry-run` is valid (report only); `--undo --limit` is a usage
   error, since partially undoing a recorded set is how inconsistency starts.

Expected: `auto` removed from 186 rows, `go` from ~20–25. `--dry-run` first.

## Component 4 — Optional reranker

Disabled by default. Contract specified rather than assumed — there is no
standard "OpenAI `/v1/rerank`":

- Request `{model, query, documents: [str], top_n}`; response
  `{results: [{index, relevance_score}]}` where `index` is a position in the
  request array.
- **Cardinality:** the endpoint returns **at most** `min(top_n, len(documents))`
  results and is *not* required to return one per document — revision 2's
  "reject on missing index" was wrong for exactly this reason. Documents whose
  index is absent keep their SQL order and are appended after the reranked
  ones.
- **Rejected wholesale** (fall back to SQL order): any index out of range or
  duplicated, any `relevance_score` that is non-numeric, NaN, or infinite, any
  non-object entry, or more results than documents submitted.
- **`top_n < limit` is defined:** rerank the top `top_n` candidates, then
  append the remaining SQL-ordered candidates to fill `limit`. The caller
  always gets `limit` results when that many exist.
- Runs after blending, before the application's final limit, over the ordered
  union (§1.4).

Ollama has no rerank endpoint; that is why this is configurable and off. It is
a seam, not a claimed feature.

## Component 5 — The user-tag guarantee, stated honestly

Revision 2 claimed "no user tag is ever destroyed". **That was still false.**
`update_memory_tags` explicitly preserves prior provenance for retained tags,
so a user who submits a complete tag list containing an existing `go` tag
keeps provenance `entity` — and retag would then remove a tag the user had
explicitly chosen to keep.

Two changes, together:

1. **Fix the semantics at the source.** `update_memory_tags` stamps
   provenance `user` for every tag the caller explicitly submits. Explicit
   submission *is* user intent; recording it as `entity` was the bug.
2. **State the residual hole.** Rows edited *before* this fix are
   indistinguishable — a `go` tag the user deliberately kept in June still
   reads as `entity` today. Nothing in the data can recover that intent.

The guarantee therefore reads: **no tag whose provenance is `user`, and no tag
with unknown or missing provenance, is ever removed. Tags explicitly retained
by a user before this fix may be removed, and `--dry-run` plus the undo file
are the mitigation.** That is weaker than revision 2's claim and is the true
one.

## Migration and the backup allowlist

`openbrain migrate` applies the two `ADD COLUMN IF NOT EXISTS` statements
**in a single transaction** and creates no other schema object — no ledger
table, no function, no sequence. `backup.py` fails on any TOC entry outside
its exact 19-entry allowlist, so a ledger table would break every backup until
`EXPECTED_TOC_ENTRIES` and its `assert len(...) == 19` were updated. Columns
create no TOC entries.

**`IF NOT EXISTS` is not sufficient validation.** If `access_count` already
exists as a nullable `bigint`, or with the wrong default, the ALTER silently
does nothing and an existence-only check passes. Both migrate and the startup
barrier validate **type, nullability, and default** via
`information_schema.columns`, and fail loudly on mismatch.

**The startup barrier must not block its own repair.** The
missing-column check is invoked by the MCP server, the API, and search paths —
explicitly **not** by `openbrain migrate` itself, or the only command that can
fix the problem would refuse to start. Verified afterwards, not assumed: run
`openbrain backup` post-migration and confirm it still verifies.

## Configuration

New `ranking` block; every key optional, absent file reproduces defaults;
per-field env override consistent with `DatabaseConfig`.

| Key | Env | Default | Validation |
| --- | --- | --- | --- |
| `ranking.w_rec` | `RANKING_W_REC` | 0.05 | finite, ≥ 0 |
| `ranking.w_imp` | `RANKING_W_IMP` | 0.03 | finite, ≥ 0 |
| `ranking.w_acc` | `RANKING_W_ACC` | 0.02 | finite, ≥ 0 |
| *(sum of the three)* | — | 0.10 | **≤ 0.10, else error** |
| `ranking.half_life_days` | `RANKING_HALF_LIFE_DAYS` | 180 | finite, > 0 |
| `ranking.access_cap` | `RANKING_ACCESS_CAP` | 10 | integer, > 0 |
| `ranking.access_tracking.enabled` | `RANKING_ACCESS_TRACKING` | true | bool |
| `ranking.rerank.enabled` | `RERANK_ENABLED` | false | bool |
| `ranking.rerank.url` | `RERANK_URL` | "" | required non-empty when enabled |
| `ranking.rerank.model` | `RERANK_MODEL` | "" | required non-empty when enabled |
| `ranking.rerank.api_key` | `RERANK_API_KEY` | "" | optional; sent as `Authorization: Bearer` when set |
| `ranking.rerank.top_n` | `RERANK_TOP_N` | 50 | integer, > 0 |
| `ranking.rerank.timeout_seconds` | `RERANK_TIMEOUT` | 5 | finite, > 0 |

Invalid configuration fails at startup with the offending key named — not at
the first search.

## Testing

The repo's import situation is mixed and must be accommodated:
`tests/test_core.py` manipulates `sys.path` and `src/main.py` imports `db.*`,
so `db.queries` and `src.db.queries` can load as distinct modules with
independently patched state. New tests import `src.<module>` and patch at the
use site; MCP tests take symbols from `src.main`.

| Area | Test |
| --- | --- |
| Fusion | k=60 both arms; a vector-only hit can outrank a lexical-only hit |
| Blending boundary | gap > 0.10 ⇒ no flip; gap < 0.10 ⇒ flip; gap == 0.10 ⇒ tie broken deterministically |
| Config | sum > 0.10 rejected; negative weight, zero half-life, zero cap rejected; rerank enabled without url/model rejected |
| Clamping | NULL `created_at` ⇒ recency 0 (not 1); future date ⇒ ≤ 1; NULL/out-of-range importance; negative access; integer ratio ≠ 0 |
| NULL ordering | NULL importance and NULL `created_at` sort last, not first |
| Lexical arm | OR rewrite; empty tsquery disables ILIKE too; `%`, `_` **and `\`** escaped |
| Branches | all four; `rel_norm` defined once; no-signal branch unblended |
| Access | `memory_get` increments, `memory_search` does not; REST GET does not; both annotations flipped; failure swallowed; kill switch |
| Reranker | missing indices appended in SQL order; duplicate/out-of-range/NaN rejected wholesale; `top_n < limit` fills to `limit`; unreachable ⇒ unchanged order |
| Tagger | each sufficient alias; each qualified context with and without its bare token; negatives: "go build a prototype", "I need to go test it", "frontend" alone; `nodejs` single canonical spelling **for generated tags only**, so a user-owned legacy `node` survives |
| Retag | user and unknown-provenance tags survive; explicit-tag-update-then-retag; provenance-only difference is selected; CAS conflict skipped; content edit then undo refused without `--force`; truncated undo file rejected before first write; concurrent run blocked by lock |
| Migration | pre-existing column with wrong type/nullability/default fails loudly; `migrate` itself is exempt from the barrier |
| **Integration** (`-m integration`) | tsquery cast, window scoring and all four branches are valid SQL; LIMIT-promotion actually promotes a pool row; `::uuid[]` update increments |

**Integration tests must not mutate the live corpus.** The access-increment
test commits its own transaction, so fixture rollback cannot contain it. Each
integration test inserts a disposable row, operates on that id only, and
deletes it in a `finally`. Mocked cursors verify shape, never syntax — sub-
project 1 shipped a zero-vector query no mocked test could validate, and the
tsquery cast and window scoring are the same trap.

## Risks and accepted risks

| Risk | Mitigation |
| --- | --- |
| OR-semantics raises recall, may lower precision | `ts_rank_cd` proximity ranking; RRF caps either arm; `--explain` |
| RRF discards magnitude | Accepted; magnitudes in `--explain` |
| `rel_norm` depends on pool membership | Documented; raw `rrf` shown |
| Pre-fix user intent on auto-provenance tags is unrecoverable | §5; `--dry-run` and undo |
| Annotation change disturbs an MCP host | Kill switch reverts both annotations and writes |
| Backfill removes a useful tag | non-`user` only, unknown fails closed, CAS, fsynced undo, `--undo` |
| Live process re-adds `auto` mid-backfill | Restart barrier before step 6 |
| Reranker never gets a backend | Accepted; disabled |

## Rollout order

1. `openbrain migrate` (+ `schema.sql`, column validation, startup barrier
   exempting migrate); **verify `openbrain backup` still passes**.
2. Manifest `database` identity in `backup.py` (needed by the retag gate).
3. Lexical arm + symmetric fusion + restructured query with limit last.
4. Blending, clamping, NULLS LAST, config validation, `--explain`.
5. `update_memory_tags` provenance fix (§5) — **before** any retag run.
6. Access tracking on `memory_get` + annotation changes + kill switch.
7. Tagger corrections. **Then restart services.**
8. `openbrain retag --dry-run`, review counts, then the real run behind the gate.
9. Reranker seam (disabled).
10. Docs: README retrieval section; query input documented as literal natural
    language.

Steps 1, 2, 5, 6, 7 mutate schema, manifest content, provenance, counters, or
what subsequent writes store; steps 3, 4, 9 are pure code. Rolling back code
does not remove the columns or restore counters — the columns are additive and
harmless when unused, which is what makes this order safe, not an absence of
mutation. Step 5 must precede step 8, or the backfill runs against the very
provenance semantics it depends on.

## Handoffs

- **Sub-project 3** inherits the corrected tagger and the fixed
  `update_memory_tags`, so captured memories are not born tagged `auto`.
- **Sub-project 4** consumes `search_memories` unchanged. A new table needs
  its TOC entries added to `EXPECTED_TOC_ENTRIES` and the `assert` updated.
