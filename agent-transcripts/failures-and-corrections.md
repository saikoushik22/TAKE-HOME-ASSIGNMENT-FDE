# Failures and corrections

Every defect found during the build, how it was detected, and the fix. Ordered
by severity of what would have shipped.

Legend — **Found by:** `run` (executing it), `test` (a test written first),
`review` (reading), `tooling` (a build or install failed).

---

## 1. Sanitizer had a dead code path with a security consequence

**Severity:** high · **Found by:** test

Two Markdown sanitizers existed. `sanitize_markdown()` used nh3 with a strict
allowlist and matched the module docstring. It was **dead code** —
`sanitize()` never called it, using its own inline regex instead:

```python
r"<\s*(script|iframe|...)\b[^>]*>(.*?)(?:<\s*/\s*\1\s*>)?"
```

The trailing group is optional and `.*?` is non-greedy, so both match empty. The
pattern removed only the opening tag. Input `<script>alert(1)</script>` became
`alert(1)</script>` — the payload preserved as text, with a sanitization report
claiming success.

**Root cause:** a second implementation added next to the first, and the
docstring describing the one that was no longer reachable.

**Fix:** one Markdown path only. Elements are removed *with their bodies*, with
separate handling for unclosed tags (fail closed to end of document), void
elements, event handlers, and dangerous URL schemes in both Markdown link syntax
and raw attributes.

**Not the obvious fix.** The tempting correction was to delete the regex and
call the nh3 version. Measuring first showed nh3 turns `> quote` into
`&gt; quote` and `a < b` into `a &lt; b` — destroying every blockquote in a
product whose main output is essays. Regexes were kept deliberately, and the
docstring rewritten to say what the code does and why.

---

## 2. Citation regex could not match the format the prompt requested

**Severity:** high · **Found by:** test

```python
# Matches [S1], [S2], and grouped forms like [S1, S3] or [S1][S2].
MARKER_RE = re.compile(r"\[S\s*(\d+(?:\s*,\s*\d+)*)\s*\]", re.IGNORECASE)
```

The comment claims `[S1, S3]`. The pattern matches `[S1, 3]` — there is no `S`
allowed on continuation numbers. The system prompt tells the model to "cite with
inline markers like [S1] or [S2]", so for a claim supported by two sources the
natural output is `[S1, S2]` — which did not match, was treated as unresolvable,
and was **stripped from the answer**.

This silently deleted *correct* grounding and lowered the exact metric the
product is judged on.

**Fix:** allow an optional `S` on continuation numbers; add one shared
`_parse_index()` tolerating the prefix, used by both the validation and
renumbering parsers, which had each rolled their own `int()` conversion.

**Lesson:** the comment was right and the code was wrong. A comment describing
behaviour is a test case that has not been written yet.

---

## 3. Startup could hang forever behind a long-running ingest

**Severity:** high · **Found by:** run

The suite began hanging indefinitely. First hypothesis — Ollama contention from
a concurrent ingest — was plausible and wrong; probing showed all three provider
health checks returning in under 0.5s.

`pg_stat_activity` gave the real answer:

```
pid 1530  idle in transaction   53 min   <- the ingest
pid 3972  active, Lock:relation 32 min   <- schema.sql, blocked by 1530
pid 5082  active, Lock:advisory  7 min   <- next startup, blocked by 3972
pid 5841  active, Lock:advisory  1 min   <- and the next
```

`CREATE TABLE/INDEX IF NOT EXISTS` still takes a relation lock even when it does
nothing. The ingest held one transaction for its entire run, so startup blocked;
and because the blocked startup already held the schema advisory lock, every
later startup queued behind it. **One slow writer wedged the whole service**,
presenting as a container that never turned healthy — with no diagnostic.

**Fix, two parts:**

1. `SET LOCAL lock_timeout = '10s'` on schema application, raising
   `DatabaseUnavailableError` with the exact query to diagnose it. Failing in
   seconds beats an unbounded wait nobody can see into.
2. The ingest now commits **per episode**. It no longer holds relation locks for
   hours, and a crash at episode 200 no longer discards the first 199.

Verified: the suite went from hanging indefinitely to **100 passed in 13.4s**.

---

## 4. Third-party corpus committed to the repository

**Severity:** high (IP + hygiene) · **Found by:** review

A post-commit check for tracked paths matching `node_modules|\.venv|\.env|data/`
found **392 transcript files, 24.9 MB** committed at `backend/data/episodes/`.

`.gitignore` had `data/transcripts/`. The ingest CLI runs with `backend/` as its
working directory, and `DATA_DIR=./data` resolved relative to *that*, so the
corpus landed in `backend/data/episodes/` — which the rule did not match.

**Fix, two parts:**

1. `.gitignore` now uses a bare `data/`, which matches at any depth.
2. The real root cause: `Settings.data_path` now resolves a relative `DATA_DIR`
   against the **repository root**, never the process working directory. The
   ingest CLI and the server run from different directories, so the same setting
   previously pointed at two places and downloaded the corpus twice.

History was rewritten (nothing had been pushed) to purge the corpus entirely:
**476 tracked files → 83**. Verified with
`git log --all --name-only | grep data/episodes` returning nothing.

---

## 5. Copying `.env.example` verbatim crashed startup

**Severity:** medium · **Found by:** run

The documented first command is `cp .env.example .env`. Doing exactly that:

```
2 validation errors for Settings
llm_fallback_provider  Input should be 'ollama', 'anthropic' or 'openai'
                       [input_value='']
ingest_max_episodes    Input should be a valid integer [input_value='']
```

`.env.example` ships optional settings as a bare `KEY=`, which is how a reader
expects to see "leave blank". Pydantic cannot coerce `''` to a typed optional.

**The worst possible first-run experience** — the documented happy path failing
on the first command.

**Fix:** a `mode="before"` validator mapping blank strings to `None` for
nullable fields. A regression test asserts it per-field, and the test env
deliberately sets those variables to `''` to reproduce the exact shape.

**Near-miss:** the first attempt included `llm_model` in that validator. It is
typed `str`, not `str | None`, so returning `None` fails type coercion before an
`after` validator can run. Caught by running it.

---

## 6. Multi-statement DDL cannot go through asyncpg

**Severity:** medium · **Found by:** run

```
asyncpg.exceptions.PostgresSyntaxError:
cannot insert multiple commands into a prepared statement
```

`conn.exec_driver_sql(ddl)` on a multi-statement `schema.sql`. asyncpg prepares
every statement, and a prepared statement holds exactly one command.

**Fix:** drop to the raw driver connection for the schema script, which uses the
simple query protocol and accepts the whole file — still inside the transaction
and the advisory lock, so atomicity is preserved.

---

## 7. FastAPI 204 route rejected at import time

**Severity:** low · **Found by:** run

```
AssertionError: Status code 204 must not have a response body
```

FastAPI infers a response model from the return annotation, and `-> None` on a
`status_code=204` route produces a model a 204 may not carry. The app failed to
build at all.

**Fix:** explicit `response_model=None` on the delete route, with a comment
naming the cause.

---

## 8. Frontend would not compile

**Severity:** low · **Found by:** tooling

```
src/lib/api.ts(12,26): TS2339: Property 'env' does not exist on type 'ImportMeta'
vite.config.ts(14,17): TS2580: Cannot find name 'process'
```

**Fix:** `src/vite-env.d.ts` with `/// <reference types="vite/client" />` and a
typed `ImportMetaEnv`; added `@types/node`. Build then produced 245 KB / 80 KB
gzipped.

---

## 9. CSS URL blocker left the host in stored content

**Severity:** low · **Found by:** test

`@import url(https://evil.test/x.css)` became
`/* blocked-remote-url */( /* blocked-remote-url */(evil.test/x.css)` — the
regex replaced only the `url(` prefix.

Contained in practice (broken syntax, plus `default-src 'none'`), but the
sanitization report claimed a removal that had not happened, and the hostile host
remained in the database.

**Fix:** separate substitution patterns consuming the entire `@import` statement
and the entire `url(...)` expression.

---

## 10. Responsive attribute on the wrong element

**Severity:** low · **Found by:** review

The mobile tab logic set `data-mobile-hidden` on a wrapper `<div>` with
`display: contents`, while the CSS targeted `.artifact[data-mobile-hidden]`. The
selector could never match, so the artifact pane would not hide on mobile.

**Fix:** pass `mobileHidden` into `ArtifactViewer` and set the attribute on the
`<aside class="artifact">` itself.

---

## 11. Docker Desktop could not start

**Severity:** blocker · **Found by:** tooling

`docker info` exited **0** while embedding `Docker Desktop is unable to start`
in its output — a check on the exit code alone reported success. Root cause:
WSL2 was not installed (`HypervisorPresent=True`, so virtualization itself was
fine; the `VirtualizationFirmwareEnabled=False` reading is the usual artifact of
a hypervisor already holding it).

**Fix:** `wsl --install --no-distribution`, then restart Docker Desktop. The
engine came up in 5 seconds without the pending reboot. Documented in the README
troubleshooting section, since a Windows evaluator will hit exactly this.

---

## 12. A working process killed on a bad diagnosis

**Severity:** process error, no code impact · **Found by:** the correction

The corpus ingest was declared hung and killed, based on the Python process
showing ~0% CPU and the database still reporting 3 episodes.

Both signals were misread:

- Python sits near 0% CPU because the embedding work happens in the **ollama**
  process — Python is blocked on HTTP.
- The database showed 3 episodes because the ingest committed only at the *end*
  of the run (bug #3), so in-progress work was invisible.

The process was working correctly. It was killed and restarted, losing several
minutes.

**Correction:** measure the signal that actually reflects progress — the
ingest's own log lines — not a proxy that happens to be nearby. This directly
motivated the per-episode commit in #3, which also makes progress visible in the
database while a run is under way.

---

## Summary

| # | Defect | Severity | Found by |
|---|---|---|---|
| 1 | Sanitizer dead path leaked script bodies | high | test |
| 2 | Citation regex stripped valid grounding | high | test |
| 3 | Startup hung behind ingest locks | high | run |
| 4 | Corpus committed to git | high | review |
| 5 | `.env.example` crashed startup | medium | run |
| 6 | asyncpg multi-statement DDL | medium | run |
| 7 | FastAPI 204 response model | low | run |
| 8 | Frontend TS build errors | low | tooling |
| 9 | CSS blocker left host in content | low | test |
| 10 | Responsive attribute misplaced | low | review |
| 11 | Docker Desktop / WSL2 | blocker | tooling |
| 12 | Killed a working process | process | correction |

**Eight of twelve were found by running or testing.** Two of the three
highest-severity code defects were in the *security* layer, and neither was
visible on reading — both files had confident, plausible docstrings describing
behaviour the code did not have.
