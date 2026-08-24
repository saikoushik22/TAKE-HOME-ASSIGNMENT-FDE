# Agent transcripts

Record of the AI-assisted build: what was delegated, what went wrong, and how
each problem was diagnosed and corrected.

This folder exists because the interesting part of AI-assisted engineering is
not the code that came out clean on the first try. It is the failures — and
specifically, whether they were caught by *running the thing* rather than by
reading it and assuming.

No secrets appear in these logs. Environment values shown are placeholders or
non-sensitive defaults.

| File | Contents |
|---|---|
| [`session-log.md`](session-log.md) | Chronological build log, phase by phase |
| [`failures-and-corrections.md`](failures-and-corrections.md) | Every defect found, its root cause, and the fix |

---

## How the work was directed

The build ran in eight phases (PRD §4). Each phase had to leave the repo in a
runnable state, so a stop at any point still yielded something demonstrable.

Three rules governed the AI-assisted portions:

**1. Nothing is "done" until it has been run.**
Every defect of consequence in this project was found by executing something,
never by reading it. The sanitizer *looked* correct and had a confident
docstring; it had a dead code path and a leaky regex. The config layer *looked*
correct; copying `.env.example` verbatim — the documented setup path — crashed
startup. Reading agrees with itself. Running does not.

**2. When code and its documentation disagree, distrust both.**
Two of the most serious defects were signposted by a comment that contradicted
the code: `MARKER_RE`'s comment claimed it matched `[S1, S3]` when the pattern
could not, and the sanitizer's module docstring described an nh3 path that was
dead code. In both cases the prose was written from intent and the code drifted.
A comment asserting a behaviour is a *test case waiting to be written*.

**3. Measure before choosing.**
Where a decision had a factual answer, it was measured rather than argued.
Whether to sanitize Markdown with nh3 was settled by running nh3 over
representative Markdown and observing that it turns `> quote` into
`&gt; quote`, destroying every blockquote. That is a five-minute experiment that
prevented shipping a feature which would have quietly corrupted every essay.

---

## Where AI assistance helped most, and least

**Most.** Breadth. Producing a coherent provider abstraction across three SDKs,
a full design system, and a large test suite in one pass — work that is
mechanical but voluminous, where consistency matters more than insight.

**Least.** Anything requiring a fact about the running system. The generated
code confidently used `exec_driver_sql` for multi-statement DDL (asyncpg cannot
— a prepared statement holds one command), assumed a 204 route could carry a
response model, and wrote a citation regex that did not match the format its own
prompt asked the model to emit. Each was plausible, idiomatic, and wrong, and
each took seconds to disprove by running it.

The pattern: AI assistance is strong at *shape* and weak at *contact with
reality*. The engineering judgment is knowing which parts of a system have sharp
edges — async drivers, lock behaviour, sanitizer bypasses, browser security
attributes — and insisting on empirical verification exactly there.

---

## The most instructive failure

Not a coding error — a **diagnostic** one.

The test suite began hanging indefinitely. The first hypothesis was Ollama
contention, since a corpus ingest was running and saturating the CPU. That
hypothesis was plausible, cheap to believe, and wrong.

Probing each provider's health check directly showed all three returning in
under half a second. The actual cause came from asking PostgreSQL rather than
guessing:

```sql
SELECT pid, state, wait_event_type, now()-xact_start AS age
FROM pg_stat_activity WHERE state <> 'idle';
```

The ingest held a single transaction open for 53 minutes. `CREATE TABLE IF NOT
EXISTS` still takes a relation lock even when it does nothing, so application
startup blocked behind it — and because the blocked startup held the schema
advisory lock, every *subsequent* startup queued behind that. One slow writer
wedged the entire service, presenting as a container that simply never turned
healthy.

Two real bugs, both shipped as fixes: `lock_timeout` on schema application so
startup fails fast with an actionable message, and per-episode commits in the
ingest so it no longer holds locks for hours or discards hours of work on a late
failure.

The lesson worth keeping: **a plausible hypothesis is not a diagnosis.** The
database could name its own blocker precisely, and asking it took less time than
reasoning about it did.
