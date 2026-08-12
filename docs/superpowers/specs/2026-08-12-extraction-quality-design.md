# OpenBrain Extraction Quality — Design (revision 4)

> **Status: draft, gate not cleared.** Three critical-review rounds have been
> run. The remaining open items are the numeric thresholds in §10, which are
> Mihai's to set, not the reviewer's. No part of this is implemented.

Auto-capture shipped 2026-08-10 and was widened on 2026-08-11 from one project
to three work trees (~88 repositories). Before that widening could take effect,
extraction was audited against real transcripts. It failed.

**Capture is disabled as of 2026-08-12** (`capture.enabled: false`, artifact
resynced, `openbrain-capture.timer` stopped and unlinked) and stays disabled
until the acceptance criteria in §8 are met. Note the flag is not yet a real
kill switch — see §7 Phase 1.1.

**Goal in one line:** what auto-capture writes should be worth reading in six
months, and it should write nothing otherwise.

## 0. What is already in the database

Capture was live from 2026-08-10 until it was disabled on 2026-08-12, and it
**did write**. Measured, not assumed:

| Measure | Value |
| --- | --- |
| Rows with `source = 'claude-code-auto'` | 4 |
| Distinct `boundary_id` | **3** |
| Distinct `session_id` | 1 (`783b16f4`) |
| Distinct `source_id` | 4 |
| `capture_state.generation` | 5 |

All four came from one session, attributed to `/home/mihai/tools/open-brain` —
the sole allowlisted project at the time.

| Stored | Content (abridged) | Judgement |
| --- | --- | --- |
| 18:19 | "The 'clear' command in Claude CLI does not write anything when the command exists" | junk — a garbled reading of a user prompt |
| 19:40 | "The hook binary validates every entry in the allowlist before matching…" | class B — that session's own work, now in git |
| 19:40 | "The dry-run bug occurred because … the 'newer event wins' branch" | class B — same |
| 19:58 | "The project uses a single branch with linear history…" | marginal — true, derivable from `git log` |

Generation 5 implies four generation-advancing store transactions against three
distinct boundaries in rows. **One generation transition is unexplained.**
Candidate explanations — a boundary committing twice, a row deleted later, an
administrative change, or a zero-row transaction from a late `source_id`
conflict (`store_boundary` advances generation even when every insert is
skipped) — are not distinguishable from the surviving rows, so none is
asserted. `committed_work_identity` holds only the latest identity and cannot
serve as an inventory; revision 2 wrongly inferred "one boundary" from it.

## 1. What was measured

30 transcripts drawn round-robin across 23 in-scope projects, run through the
real `read_exchanges → windows → extract` path with nothing written. The
evidence quote — normally discarded — was retained so each fact could be
checked against its source.

| Measure | Value |
| --- | --- |
| Transcripts audited | 30 |
| Produced no facts | 23 (77%) |
| `content_failure` (per transcript, not per call — see §8.5) | 6 (20%) |
| Facts produced | 10 |
| Facts worth keeping | **1** |

The 77% silence rate is the design working as intended. The problem is
entirely in what survives it.

### Judgement of the 10 facts

| # | Verdict | Fact (abridged) |
| --- | --- | --- |
| 1 | **keep** | Backup fails when a new table is not added to the dump TOC allowlist |
| 2 | marginal | Public bucket CORS now allows `PUT/POST/DELETE` — true, but in `git log` |
| 3 | junk | #2 restated with a different category |
| 4 | **junk — invented rationale** | `SONAR_TOKEN` lives in the Dependabot store "without interference from other CI runs" |
| 5 | junk | "The project resets its weekly limit on July 24, 4am" |
| 6 | junk | "The project uses a subagent-driven execution approach" |
| 7 | junk | "Border literals left unswept in six of the task's own files" |
| 8 | junk | "9 tasks on branch `feature/consumer-pwa-restyle`" |
| 9 | junk | "The user hit their session limit at 10:50am" |
| 10 | junk | "The user has hit their session limit at 4:50pm" (category: `root-cause`) |

## 2. Failure classes and their causes

### A. Claude Code API-error records read as conversation (facts 5, 9, 10)

Real records in the transcript, structurally unambiguous:

```
type='assistant'  isMeta=None  isSidechain=False
isApiErrorMessage=True  apiErrorStatus=429  error=<set>
text="You've hit your session limit · resets 4:50pm (Europe/Bucharest)"
```

Census of the 98 in-scope transcripts: **72 conversation-typed records carry
`isApiErrorMessage`**, 18–237 characters, every one chrome, and **zero** carry
`error` without `isApiErrorMessage`. Other texts include `Not logged in ·
Please run /login` and `API Error: The operation timed out.`

`decode_record` skips `isMeta` and `isSidechain` but has no rule for these, so
they reach the model labelled `assistant:`. Deterministically filterable.

### B. Session process narrated as project knowledge (facts 6, 7, 8)

Plans, task counts, subagent dispatch notes, in-progress status. The
instruction already says a fact must "still be useful months from now"; the
model ignores it, because a plan *sounds* like a decision. Ordinary assistant
prose, so nothing structural distinguishes it.

### C. Invented rationale (fact 4) — the dangerous one

The instruction demands `"X was chosen over Y because Z"`. When `Z` is not
obvious the model supplies one. The source states the real reason plainly:

> GitHub gives Dependabot-triggered runs access to **Dependabot secrets
> only**, *not* your regular Actions secrets. So on those runs
> `secrets.SONAR_TOKEN` resolves to empty and the Sonar scan fails.

Extracted instead: the secrets live there *"to ensure that Dependabot-specific
workflows use these values without interference from other CI runs"* — fluent,
wrong, unstated. The attached evidence quote is real but unrelated, so it
passed the verbatim guard untouched. A reader cannot tell fact 4 from fact 1
without re-reading the transcript.

### D. Near-duplicates within one boundary (fact 3)

`store_boundary` dedups within a batch by **exact** content hash, then checks
each candidate against the database. Batch items are not yet in the database,
so two differently-worded statements of the same thing both pass.

## 3. Why the existing guard does not cover this

The verbatim-evidence check stops invented *quotes*, and that is real. It
cannot stop a real quote welded to a wrong conclusion (C), text that was never
conversation (A), or true-but-worthless statements (B, D). It also checks only
`evidence[:120]`.

## 4. The bar

*Capture nothing unless durable; prefer silence to noise.* Operationally: **a
false or worthless memory costs more than a missed true one**, because there is
no rule that can clean it later.

## 5. Goals

1. Class A impossible by construction. Class D suppressed for pairs at or
   above the chosen similarity threshold — stated operationally, because
   embedding similarity cannot make semantic duplication impossible below it.
2. Class C caught before storage, or the fact is dropped.
3. Class B materially reduced against a predeclared target.
4. Quality re-measurable on demand.
5. `enabled` is a real kill switch.
6. The store contains no auto-captured row that has not passed this bar.

## 6. Non-goals

- Rewriting storage, retrieval or global dedup.
- Tier 3 worker hardening (live-allowlist revalidation, payload validation,
  safe claim primitive) — real, pre-existing, separately planned.
- Raising recall. If these changes cost true facts, that is acceptable.
- An epoch mechanism for capture state. Nothing here needs a true reset (§7.2).

## 7. Design

Ordering matters more than usual here: two of the obvious sequences corrupt
state. The order below is the constraint, not a suggestion.

### Phase 1 — make it safe to touch anything

1. **Real kill switch, first.** `drain` never reads `cfg.enabled`; config,
   artifact and timer only reduce exposure, so a manual `--drain` still runs.
   Implement and test refusal **before** the cleanup in 1.3 touches live data.

   The invariant is *not* "no filesystem mutation" — `--sync-hook-config`
   writes a file by design, and disabling the hook requires writing the
   artifact. State it narrowly: **while disabled, no hook or drain path
   mutates queue, claim, checkpoint, progress, model or database state.**

   | Entry point | Disabled behaviour |
   | --- | --- |
   | Hook enqueue | refuses (already does, via the artifact) |
   | `--drain` | refuses before `queue.drain_lock()`, which itself creates the queue dir and lock file |
   | `--drain --dry-run` | refuses; it exists to preview real drains |
   | `--audit` | **runs**; acceptance depends on it. Writes only its own report |
   | `--status`, `--sync-hook-config` | run; administrative |

   Digest enforcement moves into `verify_model`, which refuses an empty
   digest, and every production and audit model path goes through it.
   `CaptureConfig._validate` cannot express "required when a model call is
   made" — construction does not know which command will run.

2. **Seal the manifests** (before any prompt work, so the held-out set is
   genuinely held out). A path list does not reproduce a sample: transcripts
   are append-only.

   Three separate objects, because revision 3 conflated them — pinning the
   model digest inside the dataset would make Phase 5's stronger-model run
   either violate the manifest or silently rewrite a supposedly pinned field:

   | Object | Contents |
   | --- | --- |
   | **Dataset manifest** | path, session, persona, project, **end byte offset**, content hash of the evaluated prefix. Transcript identity only |
   | **Run profile** | model digest, prompt version, schema, context settings, thresholds, code revision |
   | **Audit result** | run profile + computed window hashes + per-stage counts |

   `read_exchanges` gains an end offset, which must refuse when the offset
   exceeds current file size, lands mid-record rather than after a newline, the
   prefix hash differs, or the path is a symlink or outside the configured
   transcript roots.

   Window boundaries are **not** pinned — that would let the audit bypass
   production `windows()`. Production computes them; the audit records the
   resulting hashes. Expected window hashes are frozen **per run profile** at
   the moment the baseline for that profile is recorded (Phase 1.4), since a
   changed context budget legitimately changes them.

   The held-out manifest is a sealed **ordered pool**, sized for sequential
   evaluation up front. Adding transcripts after seeing results leaks
   evaluation information.

3. **Resolve the four rows** (explicit approval required; destructive).

   Not a state reset. Resetting `generation` backward breaks the restore
   interlock: `Progress.classify` treats filesystem-above-database as "restored
   database, refuse" and database-above-filesystem as an ordinary rebase, so
   after a 5 → 1 reset, restoring a backup containing generation 5 would look
   like legitimate forward movement and silently rebase — and `capture_state`
   is inside the backup set, so that restore can resurrect the deleted rows.

   Clearing `.progress` files is equally wrong: the worker reads only
   `byte_offset` from progress, so a cleared file makes the next hook event for
   an existing session start at offset 0 and reprocess it entirely —
   contradicting §7.11's "historical boundaries are not reprocessed". Old
   `boundary_id`s inside those files are inert and can stay.

   The procedure, holding `queue.drain_lock()` across the whole of it so a
   concurrent worker cannot commit old-identity work mid-cleanup:

   1. Inventory first: row count, distinct `boundary_id`, `session_id`,
      `source_id` (recorded in §0).
   2. **Refuse unless** the filesystem checkpoint equals database generation 5;
      no reconcile intent exists; no pending, `.processing` or `.deadletter`
      queue artifact exists; and the inventory still matches.
   3. Delete **all** `claude-code-auto` rows and advance generation in **one**
      transaction, conditionally:

      ```sql
      UPDATE capture_state
         SET previous_generation = generation,
             generation = generation + 1,
             committed_work_identity = <cleanup identity>
       WHERE id = 1 AND generation = 5;
      ```

      Zero affected rows rolls back the deletion.
   4. Commit the database, **then** advance the filesystem checkpoint to 6. If
      execution stops between the two, database-ahead is a safe rebase.
   5. Write a **cleanup marker outside the database** recording minimum clean
      generation 6, the deleted `source_id`s and `boundary_id`s, and a
      timestamp. `--reconcile` must refuse when the database it is reconciling
      to predates that marker or still contains those rows.

      Without this, the 5 → 6 transition protects normal draining but not the
      documented restore workflow: a restored generation-5 backup correctly
      trips the filesystem-ahead refusal, but that refusal tells the operator
      to run `--reconcile`, which then accepts the restored database, rebuilds
      offsets from the resurrected rows, and lowers the checkpoint to 5.
      `capture_state` is inside the backup set, so the junk comes back.
   6. Preserve per-session byte offsets.

   **Zero remaining `claude-code-auto` rows is the invariant**, so "keep the
   marginal row" is not available as stated: a surviving auto row keeps its
   boundary id consumable by `boundary_already_committed`, keeps its session
   and offset usable by `--reconcile`, makes `init_state` refuse, and leaves
   acceptance criterion 6 without an unambiguous expected count. If that row is
   worth keeping, recreate it as an ordinary reviewed memory under a manual
   source and delete the auto row.

   Deleting every auto row, with an empty queue and preserved offsets, is also
   what makes the boundary-identity change in Phase 3.6 free: the historical
   consumers of boundary identity are `metadata.boundary_id`, `source_id`,
   queue claim payloads and `committed_work_identity`, and this removes all
   four. No versioned identity or migration is needed.

4. **Baseline before refactoring.** Build the audit around *current* behaviour,
   record the development-set baseline, and only then refactor. Refactoring
   first would silently change behaviour while being described as foundational.

### Phase 2 — the audit, around unchanged behaviour

5. **`openbrain capture --audit`.** Runs against a sealed manifest and reports
   per stage: raw model candidates, candidates rejected by the evidence guard,
   candidates removed by intra-batch dedup (once it exists), and candidates
   that would survive database dedup. Evidence lives in an **audit-only
   diagnostic structure**; `test_evidence_is_never_stored` stays true and
   evidence never enters stored metadata.

6. **One shared pipeline with named stages.** Aggregation, dedup, cap and
   capacity currently straddle `_drain_locked` and `store_boundary`. Factor
   them into explicit stages that audit and production both call, guarded by
   characterization tests over captured model payloads proving stage outputs
   are identical before and after. The audit must not infer stages by
   re-implementing `store_boundary`.

   **The equivalence has a stated limit.** Production checks capacity and
   generation while holding a database lock and then commits; the audit must
   not commit. The shared portion is preparation and deterministic candidate
   selection; concurrency-sensitive transactional validation is production-only
   and the audit performs a read-only estimate. Calling them "one identical
   pipeline" without this caveat overstates it.

   **The database-dedup stage is not reproducible** as a quality measure:
   `find_duplicate` searches the whole `memory` table, not only auto-captured
   rows, so ordinary manual memory additions change "would survive database
   dedup" between otherwise identical runs. It is reported as operational
   diagnostics and **excluded from the acceptance numbers**, unless run against
   a pinned snapshot whose identity is recorded.

### Phase 3 — deterministic corrections

7. **Drop API-error records** on `record.get("isApiErrorMessage") is True` —
   the dedicated boolean, identity-compared, not truthy `error` (§2A census;
   truthiness is also unsafe if a producer ever serialises `"false"`). One
   predicate shared by `decode_record` and `read_exchanges`, which today
   duplicate their rules rather than one calling the other.

   This changes `boundary_id`, because `read_exchanges` assigns `last_uuid`
   only after a record survives filtering and `boundary_id` hashes it —
   and session-limit errors are disproportionately final records. Phase 1.3
   makes that free. Regression test: a boundary ending on an API-error record.

8. **Intra-batch near-duplicate suppression, positioned correctly** — after
   window aggregation, **before** the `max_facts_per_boundary` cap. In
   `store_boundary` it is too late: with cap 3, windows yielding `A, B` and
   `A′, C` truncate to `A, B, A′`, losing the distinct fact `C`.

   **Two caps, not one.** `_validate_facts` today rejects an over-cap model
   response *wholesale, per window*, before aggregation — so a window returning
   four candidates of which two are duplicates is a `content_failure` no matter
   what happens downstream. Split them: a generous **hard safety limit** on raw
   model output per window (guarding against a runaway response), and the real
   `max_facts_per_boundary` applied only after aggregation and dedup.

   **Database duplicates must not consume cap slots.** Otherwise candidates
   `A, B, C, D` cap to `A, B, C`, all three prove to be database duplicates,
   and the novel fact `D` is lost. The cap applies to what survives database
   dedup.

   Reusing `0.92` does not make class D impossible — embedding similarity only
   suppresses pairs at or above the threshold. The observed CORS pair must be
   embedded and measured, and the threshold justified from that.

9. **Daily capacity enforced transactionally, after all dedup.** The final
   count is known only after exact dedup, semantic dedup and database dedup, so
   capacity must be rechecked against the prepared set while holding the
   `capture_state` row lock, alongside expected generation and `source_id`
   conflicts. Today the worker defers on raw `len(facts)` before dedup, so two
   duplicates with one slot left defer a boundary that would have fit, and
   `store_boundary` has no transactional capacity check despite its comment.
   Capacity needs a distinct outcome so the worker defers without consuming a
   content retry.

### Phase 4 — prompt and evidence, measured on the sealed development set

10. **Stop demanding a rationale.** The whole fact must be stated in the
    transcript; a reason may appear **only if the transcript states it**.
    Explicitly prohibit inferring motivation.

11. **Exclude session process by name** — plans, task lists, todo status, what
    the assistant did this session, progress narration, tool and UI output.
    Instruction, not enforcement; claimed only as "measured" (§8.3).

12. **Evidence as cited spans.** Require the *full* evidence string verbatim,
    not `evidence[:120]`, and validate it against the **specific retained
    exchanges cited**, not the flattened conversation where an unrelated quote
    can license an invented conclusion.

    Citations use compact per-window aliases (`r1`, `r2`) mapped internally to
    UUID and byte offset — 36-character UUIDs inline would materially inflate
    the prompt, and window sizing currently counts only role plus text, ignoring
    schema, instruction and delimiter overhead, so sessions with many short
    exchanges could be pushed past `num_ctx`. Prompt budgeting must include all
    framing overhead. A byte-offset-derived fallback id is required even though
    all 8 319 in-scope exchanges sampled do carry UUIDs.

    **Aliases collide across windows** — every window has an `r1` — so an
    aggregated candidate must carry `(window_hash, alias)` plus the alias map
    that existed when it was generated, or evidence resolves against the wrong
    exchange after aggregation.

    **Oversized exchanges need an explicit rule.** `render` and `windows` both
    admit a single exchange that exceeds the budget, and accounting for framing
    overhead does not change that. Choose one: split into cited fragments, skip
    with a reported reason, or raise a typed capacity failure. Budgeting must
    also state whether it uses a model tokenizer or a conservative bound — a
    character estimate does not port between `qwen3` and `gpt-oss`.

    The shape is `atomic claim → alias → verbatim span`, not one evidence
    string plus a list of ids: otherwise a cited record supporting one clause
    still licenses an unsupported one.

    **Withdrawn from revision 1**: a 40-character evidence minimum (existing
    tests legitimately carry facts as short as "we chose Rust"; any floor must
    come from labelled data) and rejecting `user:`-turn evidence (user turns
    state durable preferences and constraints, and `role == user` does not
    discriminate questions from knowledge).

13. **Bump `capture.prompt_version` atomically with any prompt change.** It
    feeds `source_id`. `PROMPT_VERSION` in `extract.py` is dead — nothing reads
    it — so the YAML value is authoritative and the dead constant should go.
    Explicitly: historical boundaries are **not** reprocessed,
    `boundary_already_committed` ignores prompt version and model digest, and
    no `--reprocess` command exists.

### Phase 5 — model and verifier, gated on Phase 4 numbers

14. **Evaluate a stronger extractor first.** `qwen3:4b` produced 20%
    unparseable output and the category confusions in §1; a better extractor
    may remove the need for a verifier entirely. Selection is **not** on
    precision and `content_failure` alone — it must also report per-call median
    and p95 latency, worst-case calls per boundary, drain throughput against
    queue arrival rate, and context/schema compatibility. A model that meets
    the quality bar while making the 15-minute drain fall permanently behind is
    not a candidate. Pin the winner by digest.

15. **Verifier only if still needed, and not naively.** A fresh call to the
    same deterministic 4B model is not independent: it carries the same
    causal-completion bias that produced the invented rationale, and a bare
    "yes" is unfalsifiable. A verifier worth building decomposes the fact into
    atomic propositions **including causal relations**, requires a cited alias
    and verbatim span per proposition, and rejects when any lacks support.

    Per-proposition evidence is not sufficient on its own: a verifier can
    decompose "X was selected because Y" into just "X was selected", cite that
    correctly, and never examine the causal clause. Either full semantic
    coverage of the stored sentence must be demonstrated, or — safer — the
    stored fact is **reconstructed from the individually verified atomic
    claims** rather than retaining the original broader sentence. It
    uses a different or stronger model than the extractor, and its cost is
    counted in 14's throughput budget. Outcomes are typed: a timeout is an
    infrastructure failure that **defers the whole boundary**, never a
    rejection, or an Ollama outage silently advances progress over good
    candidates.

## 8. Acceptance criteria

All thresholds are frozen **before** Phase 4 prompt work. Measured with
`--audit` against the sealed manifests; the development set is for iteration,
the held-out pool is opened once.

1. **Stage-specific regression fixtures**, not "produces nothing". A whole
   transcript producing nothing is the wrong assertion for C and D and would
   reward excessive silence:
   - **A fixture**: the synthetic API-error record yields no exchange, hence no
     candidate.
   - **C fixture**: the invented-rationale candidate is rejected; a correctly
     supported root cause from the same transcript **may survive**.
   - **D fixture**: exactly **one** representative survives dedup, not zero.

   Fixtures **inject captured stage inputs** rather than re-running the model:
   the C fixture feeds the known invented candidate straight into evidence
   validation, and the D fixture feeds the known CORS pair into the dedup
   stage. Re-running the model risks it simply not reproducing the candidate,
   so the regression would pass without ever exercising the mechanism.
2. **Zero unsupported atomic claims** among survivors — not only rationale, but
   dates, scope, actors and qualifiers — judged against cited spans. Judged at
   the extractor/verifier stage: database dedup must not be allowed to hide
   unsupported output merely because a similar row already exists.
3. **Class B measured separately** against a labelled challenge set, reported
   as a false-positive rate over a fixed denominator, against a target
   **predeclared before** Phase 4 (§10). Otherwise overall precision can
   improve purely because class A was removed while class B is untouched.

   The set contains **negatives** (session plans, task counts, subagent status)
   and **durable lookalike positives** (real decisions that resemble progress
   narration, root causes narrated by the assistant, summaries mixing both).
   Both are required because a zero-output extractor scores a perfect class-B
   false-positive rate: the metric is gameable by silence alone. Since recall is
   a non-goal (§6), the spec must either set a minimum detection requirement on
   the lookalike positives or **explicitly accept** that universal rejection
   solves the challenge set. That choice is a §10 decision.
4. **Precision as a one-sided lower confidence bound**, not a point estimate:
   two survivors at 100% is a bound near 22% and establishes nothing.
   Requires §10 decisions (bound, confidence level, survivor count, pool size).
   Two properties are fixed regardless of the numbers chosen:
   - **The sampling unit is the transcript, not the fact.** Facts from one
     transcript are correlated, so treating each survivor as an independent
     Bernoulli trial produces an invalidly narrow bound. Either score a
     transcript as successful only if every survivor from it is acceptable, or
     use cluster-aware inference, or preselect at most one candidate per
     transcript.
   - **The bound describes the sealed pool**, which was sampled round-robin for
     coverage rather than probabilistically. Generalising it to all future
     transcripts is not claimed.
5. **`content_failure` per extractor call**, with the today-baseline recomputed
   on that denominator before comparison — the reported 20% was six transcripts
   of 30, not failures per call. Pass condition: the candidate's per-call rate
   does not exceed the recomputed baseline. Because models with different
   window sizes make different numbers of calls, the **boundary completion
   rate** is gated alongside it. Verifier malformed output and verifier
   infrastructure failure are separate rates.
6. **Zero `claude-code-auto` rows** predating this work, verified by count.

**Which stage is judged** (criteria 2 and 4): precision is measured over
candidates surviving the evidence guard and verifier, *before* the cap and
before database dedup. Measuring after them would let the cap and an
incidentally-similar existing row hide unsupported output. "Marginal but
recoverable from `git log`" — fact 2 in §1 — **counts as a precision failure**
under §4's definition of durable.

Total facts may fall, and that is acceptable. Revision 3's "two good facts and
no junk passes" is **withdrawn**: it directly contradicts criterion 4, since
two successes cannot produce a meaningful lower bound.

## 9. Risks and open questions

- **Class B may resist prompting.** If instruction changes do not move the
  measured rate, the options are a durability lens in the verifier or dropping
  the `preference` category, where most of it landed. Unresolved until measured.
- **Judgement is manual** for criteria 2 and 3. Given the bar, a human read of
  a handful of facts per run is the right cost.
- **Statistical power.** A 77% silence rate makes survivors scarce, so bounding
  precision may consume a large pool. Loosening extraction to gather statistics
  faster is rejected: that optimises the measurement, not the store.
- **`--reconcile` rebuilds offsets from stored rows only**, so silent and
  all-duplicate boundaries leave no trace and can be reprocessed from older
  offsets after a restore. Out of scope, but "reprocessing cannot happen" must
  not be assumed anywhere.
- **Tier 3 remains open**: the worker does not revalidate the live allowlist,
  `release()` raises on a malformed payload, and no `--reprocess` exists.
  Separately planned; none of it blocks this spec.

## 10. Decisions required before implementation

These are judgement calls about how much silence is worth how much accuracy.
They are deliberately unset: a reviewer can say a threshold is missing, but
picking it is a product decision, and setting it after seeing results would be
choosing the target to match the outcome. Implementation does not start until
they are fixed and written into §8.

| # | Decision | Why it cannot be defaulted |
| --- | --- | --- |
| 1 | Disposition of the four existing rows: delete all four, or delete three and recreate the marginal one under a manual source | Destructive, and the "keep it as-is" option is unavailable (§7.3) |
| 2 | Precision lower bound and confidence level (proposed: one-sided 95% LCB ≥ 80%) | Sets how much evidence is needed before re-enabling |
| 3 | Required survivor count and maximum held-out pool | Determines how long acceptance takes, given a 77% silence rate |
| 4 | Outcome if the system is too silent to reach that survivor count | Otherwise acceptance can neither pass nor fail |
| 5 | Class-B false-positive target and denominator | §8.3 |
| 6 | Whether universal rejection may "solve" the class-B lookalike positives, or a minimum detection level applies | Recall is a non-goal, so this must be explicit rather than implied |
| 7 | Acceptable `content_failure` per-call rate and boundary completion rate | §8.5 |
| 8 | Latency and throughput bounds for model selection (p50/p95 per call, worst-case calls per boundary, drain throughput vs arrival rate) | A model can pass on quality and still make the 15-minute drain fall permanently behind |
| 9 | Oversized-exchange rule: split, skip, or typed failure | §7.12 |

Decisions 2–4 interact: a high bound with a low pool may be unreachable at a
77% silence rate. They should be chosen together.
