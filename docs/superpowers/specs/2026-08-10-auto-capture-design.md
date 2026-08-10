# OpenBrain Auto-Capture — Design (revision 5)

Scope: sub-project 3 of 4. Sub-projects 1 (reliability) and 2 (retrieval
quality) shipped 2026-08-10. Sub-project 4 (transcript-search MCP) follows.

**Goal in one line:** durable knowledge from Claude Code sessions lands in
OpenBrain without anyone remembering to store it.

Revision 1 assumed `Stop` was a session-level event and would have suppressed
almost everything; it also specified no generative model, and its transcript
filter would have fed tool output to that model as if it were conversation.
All three were verified wrong against this machine before revision 2.

## Measured facts (Claude Code 2.1.226, this laptop)

| Fact | Value | Consequence |
| --- | --- | --- |
| Distinct `promptId`s in one session | **8** | `Stop` is turn-level. A per-session "done" flag would capture turn 1 and permanently suppress turns 2–8 |
| `user` record content blocks in that session | **75 `tool_result`, 9 plain text, 1 `text`** | Filtering on top-level `type == "user"` feeds 75 tool payloads to the model as "conversation" |
| Records per session | 265 across 9 types | Most of the file is not conversation |
| Generative models present | `qwen3:4b`, `gemma4:12b`, `gpt-oss:20b` | The configured `qwen3-embedding:0.6b` cannot extract; a generative model must be pinned separately |

### Three extraction failures found by running it, not by reasoning about it

1. **The transcript hijacks the prompt.** With the instruction first,
   `gemma4:12b` ignored extraction entirely and *answered the conversation's
   question* — a multi-paragraph design answer about enclaves. Transcript text
   is untrusted input that contains instructions aimed at a model.
2. **Grammar-constrained output on a 12B model is unusably slow here.**
   `gemma4:12b` with a JSON schema over a 22 000-character transcript **timed
   out at 240 s**. `qwen3:4b` does the same work in **20 s**.
3. **Reasoning models return the payload in `thinking`, not `response`.**
   `qwen3:4b` produced perfect JSON in the `thinking` field and an empty
   `response`. Code reading `response` alone sees `""`, treats it as
   malformed, and captures nothing — silently, forever. `"think": false`
   fixes it.

With transcript-as-delimited-data, instruction last, `think: false` and a JSON
schema, `qwen3:4b` returned three self-contained facts in 20 s.

## The bar

Sub-project 2 spent its whole scope removing tag noise. A capture mechanism
that writes per turn recreates that in the content column, where **no backfill
can clean it because there is no rule to apply**. Capture nothing unless
durable; prefer silence to noise.

## Goals

1. Durable facts captured without an explicit request, at any point in a
   session — including the last turn.
2. A turn with no durable knowledge produces **no memory**. The common case,
   and it must be cheap and silent.
3. The hook can never slow, block, or fail a Claude Code session.
4. Everything auto-captured is identifiable and removable in bulk **from the
   live database** (§8 states what that does and does not cover).
5. Duplicate facts are suppressed on a best-effort basis, with the limits
   stated rather than promised away.

## Non-goals

Not transcript search (sub-project 4). No cloud LLM. No new MCP tools, no
retrieval changes, and exactly **one** new PostgreSQL object — the
`capture_state` singleton required to make restore detection sound (§6), with
the backup allowlist updated alongside it (§7). Not a replacement for explicit
`memory_store`.

## Component 1 — The hook

A `Stop` hook running `openbrain-capture-hook`, an absolute path invoked with
`args` and **no shell**, declared `"async": true` with a short timeout.

Claude waits for synchronous command hooks, so "never blocks" is only true if
the hook is asynchronous — revision 1's five-second alarm contradicted its own
goal, and an unhandled `SIGALRM` would have exited non-zero. Exit semantics
matter: 0 is silent, 2 feeds stderr back to the model, any other non-zero
shows stderr to the user. **This hook always exits 0 and writes nothing to
stdout or stderr.**

It imports only the standard library, reads a bounded amount of stdin, and
does one thing: append a queue entry. No database, no model, no network.

`stop_hook_active` is respected, though this hook cannot itself cause
re-entry, since it never blocks continuation.

## Component 2 — Watermarks, not "done"

Because `Stop` is per turn, progress is a **boundary, not a boolean**:

```
{session_id, byte_offset, last_record_uuid, last_prompt_id, updated_at}
```

Each drain processes only records after the recorded offset, with a small
overlap so a decision spanning a turn boundary is not split, and re-records
the new boundary. **A session is never declared finished**, because Claude can
always add another turn. `SessionEnd` may accelerate a final pass but cannot
replace `Stop`: a crash omits it.

Only complete JSONL lines are read; a trailing partial line is left for the
next drain, since the file is being appended to while we read.

## Component 3 — The queue, made crash- and concurrency-safe

Six agent configs and manual drains mean concurrent writers. The protocol:

- `session_id` is validated as a UUID before it becomes a filename, and the
  transcript path is validated against the configured Claude roots.
- Directory `0700`, files `0600`, symlinks rejected.
- Writes go to a unique temporary file, fsynced, then `os.replace`, then the
  directory is fsynced. Same-name overwrite would otherwise expose truncated
  JSON to a concurrent reader.
- Every drain takes a non-blocking global `flock` and exits quietly if held.
- Work is **claimed** by atomically renaming to a unique `.processing` name,
  and only the claimed file is deleted. Revision 1 could delete a newer hook
  event that arrived while extraction ran.
- Stale claims are recovered after a crash by age.
- Retry count and dead-letter state are explicit fields, not implied.

## Component 4 — Filtering the transcript

Parse `message.content` **blocks**; keep human text and assistant text only.
Exclude `tool_result`, `tool_use`, `thinking`, meta, sidechain and synthetic
records. Group human turns by `promptId`. Enforce size limits at complete
exchange boundaries.

**No minimum-length rule.** Revision 1 skipped sessions under six exchanges;
"always use X for Y" is durable and arrives in one turn. Length is not a
durability test.

Transcripts larger than the model budget are processed as **overlapping
incremental windows**, not one trailing slice: an early decision whose
resolution appears late would otherwise be cut in half.

## Component 5 — Extraction

Pinned: model `qwen3:4b` with its digest recorded, `think: false`, Ollama
structured-output schema, `temperature: 0`, fixed seed, explicit `num_ctx`,
input budget computed in **tokens** (conservative estimate) rather than
characters, and a hard timeout. Presence of the model is verified once per
drain, not per session.

The endpoint is pinned to a literal loopback address; a remote override is
rejected. Transcript text is passed as delimited **data**, with the
instruction after it, per the injection finding above.

Output schema requires, per candidate: `category`, `fact`, `evidence` (a
literal quote), and the record ids it came from. **The evidence must be found
verbatim in the filtered transcript or the candidate is discarded** — this is
the only mechanical defence against a plausible-sounding invention. The quote
is then dropped, not stored.

Caps: 3 facts per boundary, 1000 characters per fact, 20 sessions per drain,
and a **global daily insertion ceiling** with a kill switch.

**Ceiling and kill-switch semantics, because a half-applied boundary is worse
than a skipped one.** Both are checked *before* claiming work or invoking
Ollama. A boundary is **never partially inserted**: if its batch would exceed
remaining capacity, the batch is retained without advancing progress, without
consuming a retry, and without deleting the claim, and a durable `not_before`
is recorded at the next ceiling reset so the drain stops extracting rather
than re-running the model every 15 minutes for the rest of the day. The daily
count is derived from **committed database rows** under the drain lock, not
from an external counter that a crash could desynchronise.

Any timeout, malformed output, over-cap response, missing model, or OOM writes
**nothing** for that boundary.

## Component 6 — Idempotence, and the exact crash ordering

The ledger is a file, because a table would break backup verification (§7).
Files and PostgreSQL cannot be made atomic with each other, so the ordering is
specified rather than hoped for.

**`boundary_id` is independent of model output.** Derived from session id and
the byte offset / last record uuid only. Revision 2 leaned on `source_id`
"colliding", which is wrong twice over: there is **no unique constraint** on
`source_id`, so nothing collides, and a replay re-runs the model, which may
word the same fact differently and therefore produce a *different* hash. A
value derived from the output cannot identify work that produced output.

**The order, and what each crash window means:**

1. Claim the boundary (atomic rename, §3).
2. **Before invoking the model**, check whether `boundary_id <= durable
   progress`, or whether any committed row already carries that boundary in
   its metadata. If so, advance progress durably and delete the claim —
   without re-extracting.
3. Extract, then insert the **entire** batch in one transaction, checking
   `source_id` explicitly inside that transaction rather than relying on
   `ON CONFLICT`, which has no constraint to act on.
4. Atomically write and fsync progress through that boundary.
5. Delete the claimed file.

| Crash window | Recovery |
| --- | --- |
| Before the DB commit | Retry; nothing was written |
| After commit, before progress | Step 2 finds the boundary in the database and advances progress **without invoking the model** |
| After progress, before deletion | Step 2 sees `boundary <= progress` and deletes the stale claim without processing |
| Zero facts extracted | Progress is fsynced before deletion; a crash before that permits the accepted at-least-once replay — a model call, not a row |

The global drain lock is held across all of it.

**A restore is a hard interlock, not a recovery case.** If the database is
rolled back while file progress stays ahead, the next drain sees
`boundary <= progress`, deletes the retained claims, and never notices that
the rows they produced vanished — silent loss, and exactly the scenario
sub-project 1's backups exist for.

Revision 4 said "progress carries a generation" without defining where it
lives, which does not survive contact with either failure mode: a **static**
generation means restoring an older dump keeps the same value, so file
progress stays trusted and rolled-back facts are silently skipped; a
generation bumped **per commit** leaves the database at N+1 and the file at N
after an ordinary crash, and an unconditional mismatch rule then permanently
blocks the very automatic recovery §6 specifies. There is also no durable
place in the database to keep it under a no-new-object rule.

So the state becomes explicit, and the "no new schema object" non-goal is
**withdrawn** for exactly one object — a singleton **`capture_state`** table
updated *in the same transaction* as the captured rows, holding the current
generation, the previous generation, and the committed work identity.

Recovery rule: the worker adopts N+1 only when that record proves the pending
boundary committed. **Every other mismatch refuses to drain.** That
distinguishes a crash (recoverable, provable) from a restore (not
recoverable, must stop), which a bare counter cannot.

**This changes backup verification and that is part of the work, not a
side effect.** `EXPECTED_TOC_ENTRIES` and its `assert len(...) == 19` must be
updated, along with the backup tests. The new set is **derived empirically**
from `pg_restore -l` on a real dump after the table exists — never hand-edited
— because the entry count for a table plus its data and constraints is exactly
the kind of thing that is guessed wrong. Sub-project 1's allowlist did its job
here: it forced this to be a deliberate decision instead of a silent schema
drift.

## Component 7 — Nothing new in the schema

`backup.py` fails any dump whose TOC differs from its exact allowlist. This
design adds exactly one object, `capture_state` (§6), and therefore updates
the allowlist, its count assertion, and the backup tests **together, in the
same task**. The new entries are derived from `pg_restore -l` on a real dump
rather than predicted.

Nothing else is added: no index, no function, no sequence, and no per-fact
ledger table. Idempotence remains deterministic ids plus files; only the
restore interlock needs to be transactional with the data it guards.

## Component 8 — Secrets, scope, and what "reversible" means

Transcripts contain credentials. Prompt instructions do not prevent a model
from copying one, so the controls are mechanical:

- **Per-project opt-in.** A cwd/project allowlist gates queueing. A
  persona-wide hook otherwise captures every project that persona touches.
- Known credential formats and high-entropy strings are redacted **before**
  the extraction call, and candidate output is scanned again before embedding
  or storage.
- Errors, `--status` and journald never contain transcript bodies, candidate
  text or secrets.
- Metadata records session id and a project identifier, **not** the absolute
  transcript path.

**"Reversible" means the live database only.** `memory_bulk_delete` by
`source="claude-code-auto"` removes live rows; it does not touch retained
`pg_dump` archives, WAL, or dead tuples. A credential that reached the store
must be rotated regardless of deletion. Stated here rather than discovered
later.

## Component 9 — Retry, status, reprocessing

- Infrastructure failures (Ollama or PostgreSQL down) leave work queued under
  a **global backoff** and do **not** consume content retries. Counting an
  ordinary Ollama outage as the one permitted retry would dead-letter every
  session in the queue.
- Deterministically malformed model output gets one retry, then dead-letters.
- A new boundary is new work and does not inherit an old failure count.
- `capture --status` reports queued, claimed, deferred, retrying,
  dead-lettered, last successful drain, prompt and model version, and a
  sanitized last error — with each counter defined as lifetime or
  since-last-drain.
- `capture --reprocess` is keyed by prompt/model version — and needs its own
  **work identity** to function at all. A reprocessed boundary already
  satisfies `boundary <= progress` and usually has committed rows carrying its
  `boundary_id`, so the completion checks in §6 would delete the claim without
  ever invoking the new prompt. Reprocessing identity is therefore
  `(boundary_id, prompt_version, model_digest, reprocess_generation)`, and its
  committed-row check uses that versioned identity rather than the
  transcript-only `boundary_id`.

## Component 10 — Storage values

Auto-captured rows are stored with `tags=[]`, `tag_sources={}`, `entities={}`
and a fixed importance. The model is not asked to generate tags: sub-project 2
removed 186 noise tags, and inventing new ones from a local model is how that
returns.

## Rollout — gated on measurement, not impression

1. Build hook, queue, watermarks, filtering, extraction, dedup, batch insert,
   status, with tests. Everything ships **disabled**.
2. `capture --scan-recent --dry-run` over real transcripts — a dry-run input
   mode is required because before the hook is installed the queue is empty,
   so revision 1's rollout step could not have run at all. Dry-run claims
   nothing, writes nothing, changes no state, and hides nearest-memory content
   by default since it may contain secrets.
3. Build a **labelled corpus** from those candidates: keep/discard judgements
   with an explicit precision target and false-positive budget. "Read them and
   see" is not an acceptance criterion.
4. Install the timer with the hook still uninstalled: the queue stays empty,
   proving the worker is harmless.
5. Install the `Stop` hook in one persona, for one allowlisted project.
6. Widen only after the precision target holds.

Installation edits `settings.json` by **preserving unknown keys and existing
hooks**, adding exactly one identifiable entry, validating the resulting JSON,
and writing by atomic replacement preserving mode. Uninstall removes only that
entry.

## Deduplication — best effort, stated as such

Revision 1 promised duplicates "are not stored". That is not achievable with a
fixed threshold, so the goal is now explicit about being best-effort:

1. Normalized exact-text hash match first, which also covers rows with `NULL`
   embeddings that a vector search cannot see.
2. Candidates are compared against each other within the batch.
3. An exhaustive nearest-neighbour scan at the current scale (210 rows), since
   approximate HNSW search need not return the true nearest neighbour.
4. Cosine similarity above 0.92 drops the candidate; the threshold is a
   starting point and `--dry-run` prints similarities so it is tuned on
   evidence.

Paraphrases below the threshold will get through, and distinct-but-related
facts above it will be dropped. Both are accepted.

## Risks and accepted risks

| Risk | Mitigation |
| --- | --- |
| Turn-level firing floods the store | Watermarks with overlap, per-boundary caps, daily ceiling, kill switch |
| Transcript hijacks the extraction prompt | Delimited data, instruction last, schema-constrained output, evidence verification — all verified against a real hijack |
| Model invents plausible facts | Verbatim evidence must exist in the transcript; labelled-corpus precision gate before enabling |
| Secrets reach the model or the store | Project opt-in, pre-call redaction, post-hoc candidate scan, loopback pinning, rotate-anyway guidance |
| Crash between commit and ledger | Deterministic `source_id` makes replay collide; boundaries rebuildable from row metadata |
| Ollama outage dead-letters everything | Infrastructure failures use backoff, not content retries |
| Noise outlives its welcome | One `memory_bulk_delete` by source, scoped explicitly to the live database |

## Handoff

**Sub-project 4** owns whole transcripts. This design stores none, so the same
content cannot land in `memory` twice under two rules. If sub-project 4 adds a
table, `EXPECTED_TOC_ENTRIES` and its assertion must change with it.
