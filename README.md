# The Lenny Growth Assistant

An answering system for product and growth questions that refuses to guess.

It reads Lenny's Podcast transcripts, answers only from what it actually
retrieved, and attaches a citation to every claim that deep-links to the second
of the episode it came from. Ask it something the corpus does not cover and it
tells you so.

It runs entirely on your own machine. There is no API key to obtain, no account
to create, and nothing leaves the host unless you explicitly point it at a cloud
provider.

---

## Table of contents

1. [Why this exists](#1-why-this-exists)
2. [Five minutes to a running system](#2-five-minutes-to-a-running-system)
3. [How a question becomes an answer](#3-how-a-question-becomes-an-answer)
4. [The interface](#4-the-interface)
5. [Configuration you will actually touch](#5-configuration-you-will-actually-touch)
6. [The knowledge base](#6-the-knowledge-base)
7. [What was measured](#7-what-was-measured)
8. [The test suite](#8-the-test-suite)
9. [Day-to-day operations](#9-day-to-day-operations)
10. [When something breaks](#10-when-something-breaks)
11. [Making it yours](#11-making-it-yours)
12. [What this deliberately does not do](#12-what-this-deliberately-does-not-do)
13. [Where the rest of the documentation lives](#13-where-the-rest-of-the-documentation-lives)

---

## 1. Why this exists

Ask a general-purpose chatbot how to pick an activation metric and you get a
fluent paragraph assembled from a training set. It reads well. You cannot trace
a single sentence of it, and you certainly cannot put it in front of a room of
stakeholders and defend where it came from.

This system takes the opposite position on all three counts:

**Retrieval decides what can be said.** The model is handed evidence pulled from
the transcripts and instructed to work from that alone. Inline `[S#]` markers
map to a source panel showing the retrieved passage, the guest, and a timestamped
link. You can check the work.

**Not knowing is a first-class outcome.** When retrieval comes back below the
relevance floor, the language model is never invoked at all. The system returns a
structured "this is outside what I have" response. This is a code path with tests
behind it, not a prompt asking the model to be humble.

**The answer is not the end of the job.** Any response can be turned into a
Ship 30 for 30 essay, or into a Markdown or HTML artifact that renders in a
sandboxed panel next to the conversation.

The reasoning behind the scope, the metrics, and the things that were
consciously left out is written up in [`docs/PRD.md`](docs/PRD.md).

---

## 2. Five minutes to a running system

### What you need first

| Requirement | Version | Only needed for |
|---|---|---|
| Docker + Compose | 24+ | Everything (Windows: Docker Desktop requires WSL2) |
| Ollama | 0.3+ | Local inference — runs on the host, not in a container |
| Python | 3.11+ | Running ingestion or tests outside Docker |
| Node | 20+ | Frontend development |

Budget roughly **2.5 GB of disk for the models** and **1 GB for the database**
once the full corpus is indexed. 8 GB of RAM works; 16 GB is comfortable.

If your system drive is tight, relocate the model store before pulling anything:

```bash
setx OLLAMA_MODELS D:\ollama-models       # Windows
export OLLAMA_MODELS=/data/ollama-models  # Linux / macOS
```

### The setup

```bash
git clone https://github.com/saikoushik22/Take-Home-Assignment-FDE.git
cd Take-Home-Assignment-FDE

cp .env.example .env          # every default already works; no editing needed

ollama serve                  # leave this running in its own terminal
ollama pull llama3.2:3b
ollama pull nomic-embed-text

docker compose up -d --build  # database + backend + frontend
make ingest-smoke             # ~10 episodes, a few minutes
```

Open **http://localhost:8080**.

| What | Where |
|---|---|
| The application | http://localhost:8080 |
| Interactive API reference | http://localhost:8000/docs |
| Readiness probe | http://localhost:8000/api/health/ready |

> **Why Ollama is not containerised.** Putting it in a container would cut it off
> from the GPU on machines that have one, and duplicate a multi-gigabyte model
> store that already exists on the host. Compose wires
> `host.docker.internal` through so the backend can reach it either way.

### Confirming the setup is real

```bash
curl -s localhost:8000/api/health/ready | python -m json.tool
```

Each dependency should report `"healthy": true`. A `corpus` that is not healthy
means ingestion has not run yet.

Then put a real question to the UI — *"How should we think about choosing an
activation metric?"* Two things should happen, in this order: source chips
appear **first**, then the answer streams in with `[S#]` markers threaded through
the prose. The ordering is deliberate and explained in §4.

---

## 3. How a question becomes an answer

Rather than a component inventory, here is the path a single request takes.

```
Browser (React + Vite)
  ChatPane ──SSE──┐         ArtifactViewer
                  │           └─ <iframe sandbox="allow-scripts">, opaque origin
                  ▼
FastAPI
  api/       HTTP boundary — validation, SSE framing, one error envelope
  agent/     Orchestrator → Router → Skills
                            ├─ grounded_qa
                            ├─ ship30_essay  (SKILL.md)
                            └─ artifact
  rag/       Hybrid retrieval: pgvector + Postgres FTS, fused with RRF
  llm/       Provider registry: Ollama │ Anthropic │ OpenAI
  security/  Artifact sanitizer + CSP
  db/        SQLAlchemy async
       │                              │
       ▼                              ▼
PostgreSQL 16 + pgvector        Ollama (host :11434)
  conversations AND the           llama3.2:3b
  vector index in ONE database    nomic-embed-text
```

**1 — The router classifies intent.** Is this a question, a request for an
essay, or a request for a document? Short throwaway messages ("hi", "thanks")
are recognised as such and short-circuit here, before any model call. The router
is built so that it can never reject a request outright; the worst case is that
it picks the general question path.

**2 — Retrieval runs two searches, not one.** The query is embedded and searched
against pgvector, and simultaneously run through PostgreSQL full-text search.
The two ranked lists are combined with Reciprocal Rank Fusion. Dense search
catches paraphrase, lexical search catches proper nouns and jargon that
embeddings blur together; the fusion means neither has to be right on its own.

**3 — The relevance floor gets a veto.** If the fused results fall below
`RAG_MIN_SIMILARITY`, the request stops. No prompt is built, no model is called,
and the user gets an honest refusal. This is the single most important control
in the system.

**4 — The model writes, constrained by the evidence.** The retrieved passages go
into the prompt with instructions to cite them. The response streams to the
browser over SSE so that a slow local model shows progress instead of a spinner.

**5 — Citations are reconciled after generation.** Markers pointing at sources
that were not actually retrieved are stripped, sources that were never cited are
dropped from the panel, and the remainder are renumbered while preserving which
source is which.

### One database, on purpose

Conversation history and the vector index both live in the same PostgreSQL
instance. A dedicated vector store would have meant a second backup procedure, a
second set of failure modes, and a second thing to hand over at the end. pgvector
is sufficient at this corpus size, and "sufficient and boring" beat "specialised
and additional".

Schema, endpoint contracts, routing rules, and the security model are documented
in [`docs/architecture.md`](docs/architecture.md).

---

## 4. The interface

The frontend is deliberately quiet. Someone using this is reading dense prose for
long stretches, so the design work went into legibility and hierarchy.

**Indigo on cool slate, with squared geometry.** A single indigo accent
(`#4f46e5`) carries every interactive element; slate neutrals carry everything
else. Corner radii are 3–5px throughout, which reads as a document tool rather
than a consumer chat app. Assistant answers sit in ruled blocks instead of
floating as loose prose.

**Grounding is visible before the answer is.** Source chips render above the
response while it is still streaming. Citations shown only after the fact get
read as decoration — the moment a reader decides whether to trust an answer is
while it is still forming.

**Refusal is not styled as an error.** When the assistant declines, it uses
neutral colours. Red is reserved for genuine faults; amber only for the
sanitization notice on artifacts. A correct refusal that looks like a crash
teaches people to distrust correct behaviour.

**The rest of the rules.** System font stack, so there is no webfont round-trip
and no layout shift. Answer measure capped near 72 characters. Light and dark
themes are both CSS custom properties on `:root`, following
`prefers-color-scheme` with a manual override that persists. The streaming caret
is the only animation that runs continuously, and all motion respects
`prefers-reduced-motion`.

Interaction states, responsive behaviour, and the accessibility commitments are
in [`docs/design.md`](docs/design.md).

---

## 5. Configuration you will actually touch

Everything is environment-driven. **Switching models never requires a code
change.** Copy `.env.example` to `.env` — every value ships with a working
default, and `.env` is gitignored.

| Variable | Default | Reach for it when |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | You want `anthropic` or `openai` instead |
| `OLLAMA_MODEL` | `llama3.2:3b` | You have the RAM for `qwen2.5:7b`, which is materially better |
| `RAG_MIN_SIMILARITY` | `0.35` | Tuning the refusal threshold — see below |
| `RAG_TOP_K` | `8` | Trading answer breadth against latency |
| `LLM_TIMEOUT_SECONDS` | `180` | Sized for CPU inference; lower it on a GPU |
| `LOG_FORMAT` | `json` | `console` is far easier to read when tailing locally |

Every remaining option is documented inline in `.env.example`, including which
ones are mandatory.

### The threshold that governs honesty

`RAG_MIN_SIMILARITY` is the dial that decides what the system will and will not
attempt. Raise it and the assistant declines more often but is more reliable when
it does answer. Lower it and it stretches further from the evidence. If it is
refusing questions it plainly should handle, check what retrieval actually
returned before touching the number — a thin corpus looks identical to a
threshold set too high:

```bash
curl -s localhost:8000/api/search \
  -H 'content-type: application/json' \
  -d '{"query":"activation metric"}' | python -m json.tool
```

### Changing the model

Locally:

```bash
ollama pull qwen2.5:7b
# .env
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:7b
```

Or against a hosted provider:

```bash
# .env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Then `make restart`. Individual sessions can also be switched from the provider
badge in the UI header.

Providers you have no key for stay **visible but disabled, with the reason
displayed**. Hiding an unavailable option only converts a five-second
self-diagnosis into a support conversation.

### Embeddings are configured separately, deliberately

`EMBEDDING_PROVIDER` is independent of the chat provider. Moving chat to a hosted
model does not silently begin re-embedding the entire corpus through a metered
API — that is exactly the kind of invoice nobody agreed to.

> **A new `EMBEDDING_MODEL` means a full re-index.** Vectors produced by
> different models cannot be compared to each other. Set `EMBEDDING_DIM` to
> match — 768 for `nomic-embed-text`, 1536 for `text-embedding-3-small` — then
> run `make reset-index && make ingest`. If the dimensions disagree, retrieval
> refuses to serve and names both models rather than quietly returning
> meaningless neighbours.

### Provider fallback

Disabled by default. Answering with a different model than the one that was
selected makes results impossible to reproduce, and falling back to a cloud
provider would move local data off the machine without anyone agreeing to it.

```bash
LLM_FALLBACK_ENABLED=true
LLM_FALLBACK_PROVIDER=anthropic
```

Once enabled, it triggers **only on transport-level failures** — refused
connections, timeouts, 5xx. A malformed request or an over-long context does not
get retried elsewhere, because a second provider would fail on it identically
while obscuring the real bug. Whenever a fallback fires, the UI says so.

---

## 6. The knowledge base

Transcripts come from the public
[`ChatPRD/lennys-podcast-transcripts`](https://github.com/ChatPRD/lennys-podcast-transcripts)
repository.

**They are not vendored into this repo.** The transcripts are somebody else's
content; `make ingest` fetches them at setup time instead. Point
`TRANSCRIPT_REPO` elsewhere to index a different corpus.

```bash
make ingest          # incremental — only what actually changed
make ingest-smoke    # 10 episodes, a few minutes
make ingest-full     # re-embed the lot
make stats           # episode and chunk counts
```

Ingestion is content-hashed and incremental, so re-running it costs almost
nothing and produces the same result every time. It also **commits after each
episode**, which means a failure at episode 200 leaves the previous 199 safely
indexed rather than rolling the whole run back.

The pipeline is fetch → parse → **speaker-aware chunk** → embed → index. That
chunking step is the one that matters: transcripts are dialogue, and a
fixed-length splitter will happily cut through the middle of an answer and leave
a chunk that opens with a pronoun whose subject is in the previous chunk.
Splitting on speaker turns keeps each chunk self-contained.

> **Plan the time.** Embedding dominates — roughly **45–55 seconds per episode**
> on CPU-only hardware, so the full ~300-episode corpus runs for several hours.
> `make ingest-smoke` is genuinely enough to evaluate the system; a subset
> answers questions perfectly well.

---

## 7. What was measured

Numbers below come from the delivered build: 59 episodes indexed, `llama3.2:3b`,
16 GB RAM, 12-core mobile CPU, **no GPU**.

### Grounding

```bash
make eval                                    # generation + citation scoring
python -m scripts.evaluate --retrieval-only  # fast, no model calls
```

The harness scores a golden set containing both answerable questions and
questions deliberately outside the corpus.

| Metric | Target | Measured | Verdict |
|---|---|---|---|
| Abstention correctness | 100% | **100%** | **PASS — this is the release gate** |
| Retrieval p95 | ≤ 400 ms | **380 ms** | PASS |
| Citation-backed answers (CBAR) | ≥ 95% | **64.7%** | **MISS** |

**The safety property holds.** All 8 out-of-corpus questions were declined.
Nothing was invented.

**CBAR misses, and the cause is understood.** Five of the six failures are the
3B model producing a well-grounded answer and then omitting the `[S#]` markers.
Retrieval found the right evidence and the prose demonstrably reflects it — the
model simply did not comply with the output-format instruction. That is a known
consequence of the CPU-only demo model required by the brief
([PRD](docs/PRD.md) R3) rather than a retrieval defect. The harness exists
precisely so this can be re-measured against a stronger model:

```bash
OLLAMA_MODEL=qwen2.5:7b make eval
```

### Latency

| Interaction | First word | Complete |
|---|---|---|
| Greeting ("hi", "thanks", "who are you") | **~0.03 s** | ~0.03 s |
| Grounded question | ~15 s | ~43 s |

Routing, embedding, and hybrid retrieval together account for **under 0.1
seconds**. Retrieval is essentially free. The remainder is the local model, and
it splits into two rates:

- **Prefill runs at ~65 tokens/second.** The model has to read the retrieved
  transcripts before it can emit anything, which is what time-to-first-token
  actually measures.
- **Generation runs at ~10 tokens/second.** That is everything after.

Latency is therefore a function of how many tokens go in and how many come out —
which is exactly what these levers control:

| Change | Effect |
|---|---|
| `RAG_TOP_K=3`, `RAG_CONTEXT_CHARS_PER_CHUNK=500` | Shorter prompt, faster first word, narrower grounding |
| `QA_CITATION_REPAIR=false` | Skips the second generation pass on uncited answers |
| `OLLAMA_MODEL=llama3.2:1b` | About 3× faster, noticeably weaker answers |
| `LLM_PROVIDER=anthropic` + a key | Seconds rather than tens of seconds |

No amount of tuning makes a 3B model on a CPU fast. What the work below removed
was the *avoidable* waiting; the arithmetic above is simply the arithmetic. A
fast demo and a fully local one are one environment variable apart, and you
should pick knowingly.

### Waiting that used to exist and no longer does

- **Greetings ran the entire RAG pipeline** — an embedding, a hybrid search, and
  roughly 3,000 tokens of transcript in the prompt, in order to reply to "hi".
  Now caught before any model call: **52 s → 0.03 s**.
- **A cold model cost about 51 seconds on the first question.** Models are warmed
  in the background at startup, so the first person to use it no longer pays for
  everyone else.
- **A two-word message triggered a model round-trip purely to classify intent.**
  Messages too short to classify are now treated as exactly that, instead of
  being escalated as ambiguous.

---

## 8. The test suite

```bash
make test        # the lot
make test-unit   # no database, no model, no network
```

The suite runs in two tiers. Unit tests have no external dependencies at all and
run anywhere. Integration tests are marked and **skip cleanly** when PostgreSQL
is unavailable, so someone without Docker still gets a meaningful green run
rather than a wall of errors.

Coverage is concentrated on the failures that would actually cause harm:

| Area | What is asserted |
|---|---|
| **Artifact security** | A battery of distinct XSS and exfiltration techniques; the CSP directives; that the sandbox never combines `allow-scripts` with `allow-same-origin` |
| **Routing** | Questions, essay requests, and artifact requests each reach the right skill, and the classifier can never drop a request |
| **Citations** | Dangling markers are removed, uncited sources are not returned, renumbering preserves source identity |
| **Config and model switching** | Changing provider is configuration-only, and a local model name cannot leak into a hosted request |
| **API contracts** | A single error envelope everywhere, framework validation failures included |
| **Persistence** | Sessions hold independent context and survive a restart |

Everything that needs human eyes — rendering, streaming behaviour, responsive
layout, screen-reader paths — is scripted in
[`docs/manual-test-plan.md`](docs/manual-test-plan.md).

---

## 9. Day-to-day operations

```bash
make up        # start everything
make down      # stop, keeping the database volume
make logs      # tail all services
make health    # readiness, per dependency
make shell-db  # psql shell
make reset-all # destroy the database volume
```

### Following a single request

Logs are structured JSON on stdout. Every request carries a **correlation ID**
that follows it through routing, retrieval, the provider call, and persistence,
so one grep reconstructs an entire turn:

```bash
docker compose logs backend | grep <correlation-id>
```

Four events cover the four domains in which this system can fail:

| Event | Answers |
|---|---|
| `router.decided` | Which skill handled it, and which rule chose that skill |
| `rag.hits` / `rag.abstain` | What came back, at what scores, and whether the floor rejected it |
| `llm.completed` | Provider, model, token counts, latency, whether fallback fired |
| `artifact.sanitized` | What was stripped out of an artifact, and why |

Secrets are redacted **in the log formatter**, keyed by name — so no individual
call site is capable of forgetting to do it.

### Behaviour when things fail

| Failure | What happens |
|---|---|
| No API key for a provider | Marked unavailable; the UI disables it and shows why |
| Ollama not running | Readiness names it; the error carries the command to start it |
| Model timeout | Bounded and aborted; whatever was generated is kept |
| **Retrieval returns nothing** | **The model is never called**; a structured "not covered" response is returned |
| Database unavailable | The backend starts degraded instead of crash-looping; readiness names it |
| Corpus never ingested | Readiness reports `corpus: empty` and suggests `make ingest` |
| Embedding dimension mismatch | Retrieval refuses to serve and names both models |

---

## 10. When something breaks

<details>
<summary><b>Docker Desktop will not start (Windows)</b></summary>

This is nearly always a missing WSL2:

```powershell
wsl --install --no-distribution   # requires admin
```

Restart Docker Desktop afterwards and verify with `wsl --status`. If this
registry key exists, a reboot is still pending and Docker will keep failing:

```powershell
Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"
```
</details>

<details>
<summary><b>"Ollama is not reachable"</b></summary>

Check it is up and serving on the host:

```bash
ollama serve
curl localhost:11434/api/tags
```

From inside a container, the host is **not** `localhost`. Compose already sets
`OLLAMA_BASE_URL=http://host.docker.internal:11434` for this reason — it is the
single most common way a setup that works natively breaks under Compose.
</details>

<details>
<summary><b>"The transcript knowledge base has not been ingested yet"</b></summary>

```bash
make stats          # confirm it really is empty
make ingest-smoke
```
</details>

<details>
<summary><b>The backend never becomes healthy on startup</b></summary>

Usually an ingest is still running and holding relation locks —
`CREATE ... IF NOT EXISTS` still needs one. Startup is bounded by `lock_timeout`
and will fail with a clear message rather than hang indefinitely. Confirm with:

```sql
SELECT pid, state, wait_event_type, left(query,60), now()-xact_start AS age
FROM pg_stat_activity WHERE state <> 'idle';
```

Let the ingest finish, then `make restart`.
</details>

<details>
<summary><b>Answers take a long time</b></summary>

Expected on CPU-only hardware — see §7 for the full breakdown. The levers are a
smaller model, a lower `RAG_TOP_K`, or a hosted provider. Streaming means you
watch progress rather than a spinner.
</details>

<details>
<summary><b>The assistant refuses to answer</b></summary>

Frequently that is the correct behaviour. If it declines on subjects it clearly
should cover, the cause is either too small a corpus (ingest more episodes) or
`RAG_MIN_SIMILARITY` set too high. Inspect what retrieval found, without
involving the model at all, using the `/api/search` call in §5.
</details>

<details>
<summary><b>An artifact renders but its images and fonts are missing</b></summary>

Working as intended. Artifacts execute under `default-src 'none'` with no network
access whatsoever. Remote assets are blocked; `data:` URIs are fine. The complete
permit/block table is in [`docs/architecture.md`](docs/architecture.md) §8.
</details>

---

## 11. Making it yours

**A new model provider** — implement the `LLMProvider` protocol in
`backend/app/llm/` and register it in `registry.py`. Nothing else in the codebase
needs to know.

**A new skill** — add a directory under `backend/app/agent/skills/` containing a
`SKILL.md` that captures the methodology in prose, plus a handler implementing
`Skill`. Register it, then add routing patterns in `router.py`.

**A different Ship 30 house style** — edit
`backend/app/agent/skills/ship30/SKILL.md`. It is prose rather than code, so an
editor can rewrite the methodology without an engineer present. Restart to pick
it up.

**A different corpus** — set `TRANSCRIPT_REPO`, and adapt
`backend/app/rag/parse.py` if the transcript format differs from the current one.

**A different look** — the entire visual identity lives in
`frontend/src/styles/global.css`. Class names match the component markup exactly,
which means the theme can be replaced wholesale without touching application
logic.

---

## 12. What this deliberately does not do

Better written down now than discovered during a handover:

- **Schema is applied idempotently at startup, not through versioned
  migrations.** Correct for a single environment; inadequate the moment there
  are two.
- **Regenerating an artifact overwrites the previous one.** There is no version
  history.
- **There is no authentication.** The system assumes an internal, trusted,
  single-tenant deployment.
- **There is no claim-level entailment checking.** A small model can still
  misattribute a claim *within* correctly retrieved context. The mitigation is
  human: the citation panel shows the retrieved passage so a reader can verify
  the attribution themselves.

---

## 13. Where the rest of the documentation lives

| Document | Covers |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | User, problem, success metrics, assumptions, scope, risks |
| [`docs/architecture.md`](docs/architecture.md) | Schema, endpoints, retrieval, routing, security, deployment |
| [`docs/design.md`](docs/design.md) | UI principles, interaction states, responsive rules, accessibility |
| [`docs/manual-test-plan.md`](docs/manual-test-plan.md) | The manual UI test plan |
| [`agent-transcripts/`](agent-transcripts/) | Coding-agent session logs, including the attempts that failed |
