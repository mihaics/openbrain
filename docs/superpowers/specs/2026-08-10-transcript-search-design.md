# OpenBrain Transcript Search — Design (revision 5)

Scope: sub-project 4 of 4, the last. Sub-projects 1 (reliability), 2
(retrieval quality) and 3 (auto-capture) shipped 2026-08-10.

**Goal in one line:** find the conversation where something was discussed,
without that conversation ever entering `memory`.

## Measured context

| Fact | Value |
| --- | --- |
| Persona roots | **three**: `~/.claude`, `~/.claude-pactos`, `~/.claude-quarticle` |
| Transcripts on disk | **461** `.jsonl` files |
| Total size | **282 MB** across the two roots measured; more with the third |
| **Nested subagent transcripts** | the majority, in `<session>/subagents/agent-*.jsonl` |
| Conversation fraction | small — 40 of 265 records in a sampled session were human/assistant text; 75 of 85 `user` records were `tool_result` |
| **Full parse of 200 transcripts, measured** | **0.6 s**, every record decoded, no prefilter |
| Memories today | 212 |

Three of these were wrong in earlier revisions and were corrected by
measuring. The corpus is 282 MB, not 165 MB. Most transcripts are nested two
levels deeper than a `projects/*/*.jsonl` glob reaches — that glob finds 54
files in `~/.claude` where a recursive walk finds 205, so it would have
silently searched a fraction of the corpus and reported confident misses. And
there are **three** persona roots, not two: `.claude-pactos` exists and was
missed, taking the real total from 298 to **461**. A search that quietly omits
a persona is worse than one that refuses to run.

## The central decision: no index, no table, no embeddings

Transcript content stays **on disk and is searched in place**. Nothing is
copied into PostgreSQL.

This is the opposite of what the obvious design would do, and the reasons are
concrete:

1. **`memory` must not absorb transcripts.** Sub-project 3 deliberately stores
   distilled facts and nothing else. Ingesting 165 MB of raw conversation
   would drown 212 curated memories in their own search results — the exact
   noise problem sub-project 2 spent its scope removing, one layer up.
2. **A table costs backup verification.** Every new object must be added to
   `EXPECTED_TOC_ENTRIES` with its count assertion and tests. Sub-project 3
   paid that price once because a restore interlock genuinely needed durable
   state. Search does not: the transcripts are already durable files, and
   losing an index is free because it can be rebuilt by reading them again.
3. **Embedding 282 MB is not free and not obviously better.** At
   ~1024-dimension vectors over hundreds of thousands of message chunks, the
   embedding cost dominates, the storage exceeds the corpus it describes, and
   the questions people actually ask of transcripts — "which session did I fix
   the DST bug in" — are lexical.

Accepted consequence: search is lexical, not semantic. Stated plainly rather
than hidden, because the rest of OpenBrain *is* semantic and the difference
will surprise someone otherwise.

## Component 1 — The search

**Measured before designed:** fully parsing 200 transcripts — every record,
no shortcuts — takes **0.6 s**. The index this design refuses to build would
save nothing a user could perceive, while costing storage, staleness and a
backup allowlist entry.

A **recursive** walk over `.jsonl` files under the configured transcript
roots, traversed in **lexicographic path order** — the same order the cursor
depends on (see Bounds). Traversal order is never mtime-based.

**The filtering rule is shared; the reader is not.** `read_exchanges()` cannot
serve search: it does an unbounded `f.read()`, materialises every exchange,
discards timestamps and `cwd`, and treats *any* malformed record as a torn
final line and stops — which for a whole-file scan would silently abandon the
rest of the file. So the block-level rule is extracted into a pure
`decode_record()` used by both, and search gets its own **streaming** iterator
with deadline checks inside the file and record loops. One decoder means the
75-of-85 finding is fixed in one place; one reader would mean search inherits
incremental-boundary behaviour it must not have.

**Path safety.** Roots are canonicalised and de-duplicated when they overlap.
A candidate must resolve inside a root by `Path.relative_to`, not by string
prefix. Files are opened with **`openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`**,
falling back to a descriptor-relative walk with `openat` and `O_NOFOLLOW` on
every component. `O_NOFOLLOW` alone protects only the final component: a
project *directory* swapped for a symlink between resolution and open still
escapes the root. The descriptor is confirmed a regular file with `fstat`. Symlinked files and symlinked directory
components are skipped and reported in `truncation_reasons`.

The scan runs in `asyncio.to_thread` so the MCP event loop stays responsive,
with cooperative deadline checks rather than a cancellation that cannot
actually stop the work.

**No byte prefilter.** An earlier revision matched raw JSON bytes before
parsing, which is unsound: JSON escapes, Unicode case folding, content blocks
joined after decoding, and the `[REDACTED]` text redaction itself inserts can
all match the decoded string while being absent from the raw line, producing
**false negatives that look like clean misses**. Every candidate line is
decoded and matched on the decoded, redacted text. Measured cost of doing it
honestly: 0.6 s to fully parse 200 files, still far inside the budget.

Subagent transcripts are included — they are 71% of the corpus and contain
real working detail — but each match reports whether it came from one, since
"the assistant said X" means something different when the speaker was a
subagent.

**Matching is case-insensitive substring only. Regular expressions are not
supported.** Python's `re` can backtrack catastrophically, and a deadline
cannot interrupt it: `asyncio.wait_for` abandons the await while the worker
thread keeps burning CPU forever. Inside a stdio MCP server that wedges the
whole server. A linear-time engine or a killable subprocess would fix it;
neither is worth the complexity for a substring search, so the feature is
dropped rather than made dangerous.

Each hit returns: persona, project, session id, kind (`session` or
`subagent`), record timestamp, role, and a bounded snippet.

**Identity, derived from records rather than paths.** The project comes from
the record's own `cwd`, never from Claude's encoded directory slug — that slug
collapses both path separators and literal hyphens into hyphens and cannot be
decoded back. Timestamps come from records. **mtime plays no part in traversal**; it may
only sort the results of a page *after* the continuation cursor has been
determined, so it can never move a file across a page boundary. `since`
filters records, not files.

**Main transcripts match `root/<project-dir>/<UUID>.jsonl`.** Subagent files
are `.../subagents/agent-*.jsonl` and are included but reported as
`kind: subagent` with their parent session, because their stems are not
session UUIDs and "the assistant said X" means something different when the
speaker was a subagent.

**Bounds and pagination.** Revision 2 capped the scan at the newest 200 files
with no way to continue, which made every older transcript **permanently
unreachable** — a cap that cannot be paged past is not a bound, it is a
silent boundary on what exists.

| Bound | Default | Why |
| --- | --- | --- |
| Matches returned | 20 | A wall of hits is not an answer |
| Snippet | 240 chars | Enough to recognise, not to dump context |
| Wall-clock budget | 10 s | Partial results beat hanging |
| Files scanned | **unbounded**; the time budget terminates the scan | 200 transcripts fully parse in 0.6 s, so a file cap buys nothing and costs reachability |

There **is a per-record byte ceiling of 1 MB**, which is not the per-file cap
that was rejected. A streaming line reader must consume an entire record
before it can check any deadline, and `json.loads` cannot be interrupted
either — so one pathological tool-result line can blow both the time budget
and memory. Oversized records are skipped with an explicit truncation reason.

There is **no per-file byte cap**. Revision 2 had one without saying which 8 MB
it kept: the head hides the newest turns of a long session, the tail hides the
earlier discussion and starts mid-record, and skipping hides everything. With
no cap and a time budget, the question does not arise.

Every incomplete result is reported. `truncated` is accompanied by
`truncation_reasons` naming each cause — deadline, match limit, unreadable
file, malformed record, file replaced mid-scan, skipped symlink — plus
`scanned_files`, `elapsed_ms` and a `next_cursor`. A false negative reported as
complete is the worst outcome this tool can produce, so anything less than a
whole clean scan says so.

**The cursor is `(path, record_index)` plus a hash of the query and every
filter** — *not* mtime. Ordering by mtime is ordering by a mutable key:
appending to any transcript moves it across the cursor boundary, so a page can
duplicate or skip records. Traversal order is therefore **lexicographic by
path**, which does not change when a file is written to. mtime still decides
which files are *presented* first within a page, but never the traversal.
A cursor whose query/filter hash does not match is rejected rather than
silently reinterpreted against a different search.

## Component 2 — The MCP tool

One new tool, `transcript_search`, annotated **read-only and idempotent** —
which is true here, unlike `memory_get`, because it writes nothing at all.

```
transcript_search(query, limit=20, project=null, persona=null,
                  since=null, cursor=null, include_subagents=true)
```

Returns `{matches: [{persona, project, kind, session_id, agent_id,
timestamp, role, snippet}], scanned_files, elapsed_ms, truncated,
truncation_reasons, next_cursor}`.

For `kind: session`, `session_id` is the session UUID and `agent_id` is null.
For `kind: subagent`, `session_id` is the **parent** session UUID and
`agent_id` is the `agent-*` stem. Both are required: returning only the parent
collapses every subagent of a session together, and returning only the stem
gives something that is not a session id at all.

`project` and `persona` **filter metadata; they are never joined into a
filesystem path.**

**Dispatched before the `_DB_READY` guard.** `call_tool` currently rejects
every tool when the database is unavailable, which would disable a disk-only
search at exactly the moment someone is trying to work out what broke.

It deliberately does **not** return whole sessions. The answer to "which
conversation was that" is a pointer; reading the transcript is a separate,
deliberate act with a path the user already has.

## Component 3 — Secrets

**Revision 2's threat model was wrong.** It claimed transcripts "never leave
the machine" because the tool is local. They do: a snippet returned over MCP
enters the calling agent's context, and that agent may be a cloud model. This
is a **disclosure boundary**, not a best-effort local nicety.

Consequently:

- **Redaction applies to the whole accepted block before matching and before
  clipping.** Redacting an already-clipped 240-character snippet is unsound —
  clipping can remove the prefix a pattern needs to be recognised, so a
  half-visible credential survives.
- The shipped redactor is **seven credential-shape patterns with no general
  high-entropy detection**, which is weaker than revision 2 implied. This
  sub-project adds an entropy check for long unbroken tokens, and the honest
  statement stays: pattern-based redaction is incomplete, and an unrecognised
  secret format can still reach the agent.
- Refusing credential-shaped **queries** is near-theatre and is kept only as a
  cheap guard: it does nothing for a benign query whose surrounding context
  contains a secret, which is the actual exposure path.

## Non-goals

- No semantic search, no embeddings, no index, **no new PostgreSQL object**.
- No writes of any kind: this sub-project adds no row to `memory`.
- Not a replacement for `memory_search`. Memories are curated knowledge;
  transcripts are the raw record. They answer different questions and stay
  separate.

## Risks and accepted risks

| Risk | Mitigation |
| --- | --- |
| Scanning is slow | Measured at 0.6 s to fully parse 200 transcripts with no prefilter; time budget plus cursor, never an unpageable file cap |
| Regex wedges the server | Regex is not offered |
| Older transcripts unreachable | Cursor pagination; no file cap |
| A persona root is missed | All three configured explicitly; a missing root is an error, not a silent omission |
| Lexical-only surprises a semantic-search user | Documented explicitly in the tool description and README |
| A secret appears in a snippet | Redaction of the whole block before matching and clipping, plus an entropy check. Accepted and stated: pattern-based redaction is incomplete, and snippets **do** leave the machine into the calling agent's context |
| Transcript format drifts | Filtering reuses the shipped module, so one fix covers capture and search together |
| Results dwarf `memory_search` in usefulness | Separate tool, separate name; neither can silently absorb the other's job |
