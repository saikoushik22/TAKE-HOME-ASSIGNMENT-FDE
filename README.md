# The Lenny Growth Assistant

Grounded product and growth answers from Lenny's Podcast transcripts, with a
Ship 30 for 30 essay skill and a sandboxed artifact viewer.

Every claim the assistant makes carries a citation you can open and check. When
the corpus does not cover a question, it says so instead of inventing an answer.

**Runs fully local.** No API keys required — the default configuration uses
Ollama on your machine.

---

## Contents

- [What this is](#what-this-is)
- [Quick start](#quick-start)
- [Architecture at a glance](#architecture-at-a-glance)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Switching models](#switching-models)
- [Knowledge base](#knowledge-base)
- [Running tests](#running-tests)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Extending the system](#extending-the-system)
- [Documentation map](#documentation-map)

---

## What this is

A product/growth practitioner asks a hard question. A general chatbot answers
fluently from memory — confident, unattributable, and impossible to defend in a
strategy review. This assistant instead:

1. **Answers strictly from Lenny's Podcast transcripts**, with inline citations
   that deep-link to the exact second of the source episode.
2. **Declines when the corpus does not support an answer**, as a designed state
   rather than a hedge.
3. **Turns answers into publishable work** — a Ship 30 for 30 essay, or a
   Markdown/HTML artifact rendered beside the chat.

The design rationale, trade-offs, and what was deliberately left out are in
[`docs/PRD.md`](docs/PRD.md).

---

## Quick start

```bash
git clone https://github.com/saikoushik22/TAKE-HOME-ASSIGNMENT-FDE.git
cd TAKE-HOME-ASSIGNMENT-FDE

cp .env.example .env          # safe defaults; no editing needed

ollama serve                  # in a separate terminal
ollama pull llama3.2:3b
ollama pull nomic-embed-text

docker compose up -d --build  # db + backend + frontend
make ingest-smoke             # ~10 episodes, a few minutes
```

Then open **http://localhost:8080**.

| Service | URL |
|---|---|
| UI | http://localhost:8080 |
| API docs | http://localhost:8000/docs |
| Readiness | http://localhost:8000/api/health/ready |

> **Ollama runs on your host, not in a container.** Containerising it would
> forfeit GPU access where a GPU exists and duplicate a multi-gigabyte model
> store. Compose maps `host.docker.internal` so the backend can reach it.

### Verify it works

```bash
curl -s localhost:8000/api/health/ready | python -m json.tool
```

Every dependency should read `"healthy": true`. If `corpus` is unhealthy, run
`make ingest`.

Then ask the UI: *"How should we think about choosing an activation metric?"*
You should see source chips appear **before** the answer text, and inline
citations in the prose.

---

## Architecture at a glance

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

**One database on purpose.** Conversations and the vector index both live in
PostgreSQL. A second datastore for vectors would mean two backup stories, two
failure modes, and two things to explain at handoff.

Full detail — schema, endpoints, retrieval flow, agent routing, security — is in
[`docs/architecture.md`](docs/architecture.md).

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker + Compose | 24+ | On Windows, Docker Desktop needs **WSL2** (`wsl --install`) |
| Ollama | 0.3+ | Runs on the host |
| Python | 3.11+ | Only for running ingestion/tests outside Docker |
| Node | 20+ | Only for frontend development |

**Disk:** ~2.5 GB for models, ~1 GB for the database with the full corpus.
**RAM:** 8 GB minimum, 16 GB comfortable.

> Models are large. To keep them off a small system drive:
> `setx OLLAMA_MODELS D:\ollama-models` (Windows) or
> `export OLLAMA_MODELS=/data/ollama-models` (Linux/macOS).

---

## Configuration

Everything is environment-driven; **no code change is ever needed to switch a
model**. Copy `.env.example` to `.env` — every value already has a working
default, and `.env` is gitignored.

### The variables that matter most

| Variable | Default | Why you would change it |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | Switch to `anthropic` or `openai` |
| `OLLAMA_MODEL` | `llama3.2:3b` | `qwen2.5:7b` is notably better if you have the RAM |
| `RAG_MIN_SIMILARITY` | `0.35` | **The safety knob.** Below this, retrieval returns nothing and the model is never called |
| `RAG_TOP_K` | `8` | Evidence chunks per answer. Higher = slower, more context |
| `LLM_TIMEOUT_SECONDS` | `180` | Sized for CPU inference |
| `LOG_FORMAT` | `json` | `console` is easier to read when tailing locally |

`.env.example` documents every option inline, including which are required.

---

## Switching models

### Local (default)

```bash
ollama pull qwen2.5:7b
# .env
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:7b
```

### Cloud

```bash
# .env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Then `make restart`. You can also switch per session from the provider badge in
the UI header.

**Providers with no key are shown disabled *with the reason attached*, never
hidden** — a silently missing option becomes a support ticket.

### Embeddings stay local

`EMBEDDING_PROVIDER` is deliberately independent of the chat provider. Switching
chat to a cloud model does **not** start re-embedding the corpus through a paid
API — that would be a cost surprise nobody asked for.

> **Changing `EMBEDDING_MODEL` requires a full re-index.** Vectors from
> different models are not comparable. Update `EMBEDDING_DIM` to match
> (`nomic-embed-text` → 768, `text-embedding-3-small` → 1536), then
> `make reset-index && make ingest`. The app refuses to serve retrieval on a
> mismatch rather than returning meaningless neighbours.

### Fallback behaviour

Off by default. Silently answering with a different model than the one selected
makes results irreproducible, and a cloud fallback would ship local data off the
machine without consent.

```bash
LLM_FALLBACK_ENABLED=true
LLM_FALLBACK_PROVIDER=anthropic
```

When enabled it fires **only on transport failures** (connection refused,
timeout, 5xx). A semantic failure (bad request, context too long) does not retry
elsewhere — another provider would fail identically and hide the real bug. Any
fallback is surfaced in the UI.

---

## Knowledge base

Transcripts come from the public
[`ChatPRD/lennys-podcast-transcripts`](https://github.com/ChatPRD/lennys-podcast-transcripts)
repository.

**The corpus is not vendored into this repo.** It is third-party content, and
`make ingest` fetches it at setup time. Set `TRANSCRIPT_REPO` to point at a
different corpus.

```bash
make ingest          # incremental — only what changed
make ingest-smoke    # 10 episodes, a few minutes
make ingest-full     # re-embed everything
make stats           # episode/chunk counts
```

Ingestion is **incremental and content-hashed**, so re-running is cheap and
idempotent, and it **commits per episode** — a failure at episode 200 does not
discard the first 199.

> **Timing.** Embedding is the bottleneck: roughly **45–55 seconds per episode**
> on CPU-only hardware. The full ~300-episode corpus takes several hours. Start
> with `make ingest-smoke`; a subset answers questions perfectly well.

Pipeline: fetch → parse → **speaker-aware chunk** → embed → index. Chunking
splits on speaker turns rather than fixed sizes, because transcripts are
dialogue: a naive splitter cuts mid-answer and produces chunks starting with a
pronoun whose referent is in the previous chunk.

---

## Running tests

```bash
make test        # everything
make test-unit   # no database or model required
```

Two tiers. **Unit** tests need no database, no model, no network — they run
anywhere. **Integration** tests are marked and **skip cleanly** when PostgreSQL
is absent, so a contributor without Docker still gets a green, meaningful suite.

Coverage focuses on the things that would actually hurt:

| Area | What is asserted |
|---|---|
| **Artifact security** | A suite of distinct XSS/exfiltration techniques; CSP directives; the sandbox never pairs `allow-scripts` with `allow-same-origin` |
| **Routing** | Questions, essay requests, and artifact requests route correctly; the classifier never breaks a request |
| **Citations** | Dangling markers are stripped; only cited sources are returned; renumbering preserves source identity |
| **Config / model toggle** | Provider swap is config-only; a local model name cannot leak into a cloud request |
| **API contracts** | One error envelope everywhere, including framework validation failures |
| **Persistence** | Sessions keep independent context and survive restarts |

The manual UI checks — rendering, streaming, responsive layout, screen-reader
paths — are in [`docs/manual-test-plan.md`](docs/manual-test-plan.md).

### Grounding evaluation

```bash
make eval                                    # full: generation + citations
python -m scripts.evaluate --retrieval-only  # fast: no model calls
```

Scores a golden set of in-corpus and deliberately out-of-corpus questions.
Measured on the delivered build (59 episodes, `llama3.2:3b`, CPU-only):

| Metric | Target | Measured | |
|---|---|---|---|
| Abstention correctness | 100% | **100%** | **PASS — release gate** |
| Retrieval p95 | ≤ 400 ms | **380 ms** | PASS |
| CBAR (citation-backed answers) | ≥ 95% | **64.7%** | **MISS** |

**The safety gate holds.** All 8 out-of-corpus questions were declined and
nothing was fabricated.

**CBAR misses, for a specific and documented reason.** 5 of 6 failures are the
3B model writing a well-grounded answer and omitting the `[S#]` markers —
retrieval found the right evidence and the prose reflects it; the model simply
did not follow the output-format instruction. This is the documented cost of the
mandated CPU-only demo model ([PRD](docs/PRD.md) R3), not a retrieval defect.

Re-measure after switching models — that is what the harness is for:

```bash
OLLAMA_MODEL=qwen2.5:7b make eval
```

---

## Performance

Measured on the reference machine (16 GB RAM, 12-core mobile CPU, **no GPU**,
`llama3.2:3b`):

| Interaction | Time to first word | Total |
|---|---|---|
| Greeting ("hi", "thanks", "who are you") | **~0.03 s** | ~0.03 s |
| Grounded question | ~15 s | ~43 s |

**Where the time goes.** Routing, embedding, and hybrid retrieval together take
**under 0.1 s** — retrieval is effectively free. Everything else is the local
model:

- **Prompt prefill ~65 tokens/second.** The model must read the retrieved
  transcripts before writing a word. This is time-to-first-token.
- **Generation ~10 tokens/second.** This is the rest.

So latency is governed almost entirely by *how many tokens go in and come out*,
which is what the dials below control.

### Making it faster

| Change | Effect |
|---|---|
| `RAG_TOP_K=3`, `RAG_CONTEXT_CHARS_PER_CHUNK=500` | Shorter prompt → faster first word, narrower grounding |
| `QA_CITATION_REPAIR=false` | Skips a second generation on answers that came back uncited |
| `OLLAMA_MODEL=llama3.2:1b` | Roughly 3× faster, noticeably weaker answers |
| `LLM_PROVIDER=anthropic` + an API key | Seconds instead of tens of seconds |

**Honest summary.** A 3B model on CPU is slow, and no amount of tuning changes
that — the fixes above removed the *avoidable* waiting (greetings that ran the
full pipeline, a cold model on the first request, more context than the answer
needed), not the arithmetic. If you want a fast demo, switch the provider; if
you want a fully local one, expect tens of seconds and let the streaming UI show
progress. Both are one environment variable apart.

### Things that used to be slow, and are not any more

- **Greetings ran the full RAG pipeline** — an embedding, a hybrid search, and
  ~3,000 tokens of transcript in the prompt, to answer "hi". Now short-circuited
  before any model call: **52 s → 0.03 s**.
- **A cold model cost ~51 s on the first question.** Models are now warmed at
  startup in the background, so the first user does not pay it.
- **A two-word message triggered a model round-trip just to classify intent.**
  Too short to classify is now treated as such, not as ambiguous.

---

## Operations

```bash
make up        # start everything
make down      # stop (keeps the database volume)
make logs      # tail all services
make health    # readiness with per-dependency detail
make shell-db  # psql shell
make reset-all # destroy the database volume
```

### Observability

Structured JSON logs to stdout. Every request carries a **correlation ID**
propagated through routing, retrieval, provider calls, and persistence — one
grep reconstructs a whole turn:

```bash
docker compose logs backend | grep <correlation-id>
```

Four events cover the four failure domains the system can have:

| Event | Tells you |
|---|---|
| `router.decided` | Which skill, and which rule decided it |
| `rag.hits` / `rag.abstain` | What was retrieved, scores, whether the floor rejected it |
| `llm.completed` | Provider, model, tokens, latency, whether fallback fired |
| `artifact.sanitized` | What was stripped from an artifact and why |

Secrets are redacted **at the log formatter**, by key name — so a call site
cannot forget to do it.

### Resilience

| Failure | Behaviour |
|---|---|
| No API key | Provider marked unavailable; UI disables it with the reason |
| Ollama down | Readiness names it; actionable error with the start command |
| Model timeout | Bounded and aborted; partial output preserved |
| **Empty retrieval** | **Model is never called**; structured "not covered" response |
| Database down | Backend starts *degraded* rather than crash-looping; readiness names it |
| Corpus not ingested | Readiness reports `corpus: empty` with `make ingest` as the hint |
| Embedding mismatch | Retrieval refuses to serve and names both models |

---

## Troubleshooting

<details>
<summary><b>Docker Desktop will not start (Windows)</b></summary>

Almost always missing WSL2:

```powershell
wsl --install --no-distribution   # needs admin
```

Then restart Docker Desktop. Check with `wsl --status`. A reboot may be needed
if `Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"` exists.
</details>

<details>
<summary><b>"Ollama is not reachable"</b></summary>

```bash
ollama serve
curl localhost:11434/api/tags
```

From inside a container the host is **not** `localhost`. Compose sets
`OLLAMA_BASE_URL=http://host.docker.internal:11434` for you — this is the single
most common reason a working local setup breaks under Compose.
</details>

<details>
<summary><b>"The transcript knowledge base has not been ingested yet"</b></summary>

```bash
make stats     # confirm it is empty
make ingest-smoke
```
</details>

<details>
<summary><b>Backend hangs on startup / never becomes healthy</b></summary>

Usually a long-running ingest holding relation locks — `CREATE ... IF NOT
EXISTS` still needs a lock. Startup is bounded by `lock_timeout` and will fail
with a clear message rather than hang forever. To confirm:

```sql
SELECT pid, state, wait_event_type, left(query,60), now()-xact_start AS age
FROM pg_stat_activity WHERE state <> 'idle';
```

Wait for the ingest to finish, then `make restart`.
</details>

<details>
<summary><b>Answers are slow</b></summary>

Expected on CPU-only hardware: ~2–3s to first token, 20–45s for a full answer.
Options: a smaller model (`llama3.2:3b`), lower `RAG_TOP_K`, or switch to a
cloud provider. Streaming means you see progress rather than a spinner.
</details>

<details>
<summary><b>The assistant declines to answer</b></summary>

That is often correct behaviour — the corpus genuinely does not cover it. If it
declines on topics it *should* know, either the corpus is too small (ingest more
episodes) or `RAG_MIN_SIMILARITY` is too high. Check what retrieval actually
found, without involving the model:

```bash
curl -s localhost:8000/api/search \
  -H 'content-type: application/json' \
  -d '{"query":"activation metric"}' | python -m json.tool
```
</details>

<details>
<summary><b>Artifact renders but images/fonts are missing</b></summary>

Working as designed. Artifacts run under `default-src 'none'` with no network
access. Remote assets are blocked; `data:` URIs work. See
[`docs/architecture.md`](docs/architecture.md) §8 for the full permit/block table.
</details>

---

## Extending the system

**Add a model provider** — implement the `LLMProvider` protocol in
`backend/app/llm/`, register it in `registry.py`. Nothing else changes.

**Add a skill** — create a directory under `backend/app/agent/skills/` with a
`SKILL.md` (the methodology, editable without touching code) and a handler
implementing `Skill`, then add one line to the skill registry. Add routing
patterns in `router.py`.

**Change the Ship 30 house style** — edit
`backend/app/agent/skills/ship30/SKILL.md`. It is prose, not code, so an editor
can change it without an engineer. Restart to pick up changes.

**Use a different corpus** — set `TRANSCRIPT_REPO`, and adjust the parser in
`backend/app/rag/parse.py` if the transcript format differs.

### Known limitations

Stated plainly rather than discovered later:

- Schema is applied idempotently at startup, not via versioned migrations —
  correct for one environment, insufficient for two.
- Artifact regeneration overwrites rather than versioning.
- No authentication. The system assumes an internal, trusted, single-tenant
  deployment.
- No claim-level entailment checking, so a small model can still misattribute a
  claim *within* correctly retrieved context. The citation panel showing the
  retrieved text is the human-in-the-loop mitigation.

---

## Documentation map

| Document | What it covers |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | User, problem, success metrics, assumptions, scope, risks |
| [`docs/architecture.md`](docs/architecture.md) | Schema, endpoints, retrieval, routing, security, deployment |
| [`docs/design.md`](docs/design.md) | UI principles, interaction states, responsive, accessibility |
| [`docs/manual-test-plan.md`](docs/manual-test-plan.md) | Manual UI test plan |
| [`agent-transcripts/`](agent-transcripts/) | Coding-agent session logs, including failed attempts |
