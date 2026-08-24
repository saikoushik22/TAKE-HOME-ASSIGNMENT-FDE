# Architecture — The Lenny Growth Assistant

**Audience:** the client engineer who inherits this system.
**Companion docs:** `docs/PRD.md` (why), `docs/design.md` (UI/UX), `README.md` (how to run).

---

## 1. System overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Browser (React + Vite)                                                  │
│                                                                          │
│   ChatPane ──────────────── SSE ───────────────┐   ArtifactViewer        │
│   SessionSidebar                               │   └─ <iframe sandbox>   │
│   ProviderBadge                                │      (opaque origin)    │
└────────────────────────────────────────────────┼─────────────────────────┘
                                                 │ HTTP / text-event-stream
┌────────────────────────────────────────────────▼─────────────────────────┐
│  FastAPI  (backend/app)                                                  │
│                                                                          │
│   api/         HTTP boundary — validation, serialization, errors         │
│      │                                                                   │
│   agent/       Orchestrator ── Router ── Skills                          │
│      │            │              │         ├─ grounded_qa                │
│      │            │              │         ├─ ship30_essay  (SKILL.md)   │
│      │            │              │         └─ artifact                   │
│      │            │                                                      │
│   rag/        Retriever (hybrid: vector + lexical, RRF fused)            │
│   llm/        Provider registry ── Ollama │ Anthropic │ OpenAI           │
│   security/   Artifact sanitizer (allowlist + CSP injection)             │
│   db/         SQLAlchemy async — sessions, messages, artifacts           │
└──────────┬──────────────────────────────────────┬────────────────────────┘
           │                                      │
┌──────────▼──────────────┐          ┌────────────▼─────────────┐
│ PostgreSQL 16 + pgvector│          │ Ollama  (host, :11434)   │
│  sessions, messages,    │          │  llama3.2:3b  (chat)     │
│  artifacts, episodes,   │          │  nomic-embed-text (embed)│
│  chunks (HNSW + GIN)    │          └──────────────────────────┘
└─────────────────────────┘
```

**One database.** Conversations *and* the vector index both live in PostgreSQL. The brief
requires Postgres for persistence; adding a second datastore for vectors would mean two
backup stories, two failure modes, and two things to explain at handoff. `pgvector` makes
that unnecessary at this corpus size (assumption A5).

---

## 2. Component boundaries

Strict, one-directional dependency flow. Nothing below calls anything above it.

| Layer | Package | Owns | Must not |
|---|---|---|---|
| HTTP | `app/api` | Routing, validation, status codes, SSE framing | Contain business logic or touch the DB session directly |
| Contracts | `app/schemas` | Pydantic request/response models | Import from `db` or `agent` |
| Orchestration | `app/agent` | Intent routing, skill execution, context assembly | Know about HTTP or SQL |
| Retrieval | `app/rag` | Chunking, embedding, hybrid search, scoring | Know about sessions or skills |
| Models | `app/llm` | Provider adapters, fallback chain, streaming | Know about retrieval or persistence |
| Security | `app/security` | Sanitization, CSP construction | Depend on anything else |
| Persistence | `app/db` | Schema, sessions, repositories | Contain domain rules |
| Config | `app/core` | Settings, logging, errors, correlation IDs | Import from any of the above |

The practical test: `app/agent` can be unit-tested with a fake provider and a fake
retriever and no database. That property is what makes the skills independently testable,
and it is worth defending in review.

---

## 3. Database schema

PostgreSQL 16 with the `vector` extension. Applied idempotently at startup from
`backend/app/db/schema.sql`; a migration tool would be the right call once the client has
a second environment, and `docs/architecture.md` §10 says so.

### 3.1 Conversation tables

```sql
CREATE TABLE sessions (
    id             UUID PRIMARY KEY,
    title          TEXT        NOT NULL DEFAULT 'New chat',
    user_id        TEXT        NOT NULL DEFAULT 'local-user',
    user_metadata  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    provider       TEXT        NOT NULL,
    model          TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ
);

CREATE TABLE messages (
    id               UUID PRIMARY KEY,
    session_id       UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,           -- monotonic within a session
    role             TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content          TEXT NOT NULL,
    skill            TEXT,                       -- which skill produced it
    citations        JSONB NOT NULL DEFAULT '[]'::jsonb,
    retrieval_trace  JSONB,                      -- chunk ids, scores, timings
    provider         TEXT,
    model            TEXT,
    token_usage      JSONB,
    latency_ms       INTEGER,
    error            JSONB,                      -- structured, when a turn failed
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

CREATE TABLE artifacts (
    id                  UUID PRIMARY KEY,
    session_id          UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id          UUID REFERENCES messages(id) ON DELETE SET NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('markdown','html')),
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,   -- SANITIZED. This is what the viewer renders.
    raw_content         TEXT,            -- pre-sanitization, for debugging only
    sanitization_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Two decisions worth calling out:

- **`retrieval_trace` is persisted per message.** When someone reports "this answer was
  wrong", the first question is *what did it retrieve?* Storing the trace turns a
  three-hour reproduction into a single SQL query. It is the highest-leverage
  observability decision in the schema.
- **`raw_content` is kept alongside sanitized `content`.** If the sanitizer eats something
  it should not have, you need the original to prove it. The viewer *never* reads this column.

### 3.2 Knowledge base tables

```sql
CREATE TABLE episodes (
    id                UUID PRIMARY KEY,
    external_id       TEXT NOT NULL UNIQUE,   -- upstream repo path; the traceability key
    title             TEXT NOT NULL,
    guest             TEXT,
    episode_url       TEXT,
    published_at      DATE,
    content_hash      TEXT NOT NULL,          -- drives incremental re-ingestion
    source_updated_at TIMESTAMPTZ,
    token_count       INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chunks (
    id              UUID PRIMARY KEY,
    episode_id      UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    speaker         TEXT,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    token_count     INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    embedding       vector(768),            -- dimension pinned by EMBEDDING_DIM
    embedding_model TEXT NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (episode_id, chunk_index)
);

CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_idx       ON chunks USING GIN (tsv);
```

> **Operational trap, documented deliberately.** The `vector(768)` dimension is fixed at
> schema-creation time and matches `nomic-embed-text`. Switching to an embedding model with
> a different dimension (e.g. OpenAI `text-embedding-3-small` at 1536) requires a schema
> change **and** a full re-index — embeddings from different models are not comparable.
> The app refuses to serve retrieval when `EMBEDDING_MODEL` disagrees with the
> `embedding_model` recorded on indexed chunks, and says exactly that in the readiness
> probe. Failing loudly here is much better than silently returning nonsense neighbours.

---

## 4. Ingestion and retrieval flow

### 4.1 Ingestion pipeline

```
upstream repo (ChatPRD/lennys-podcast-transcripts)
   │  git archive / tarball download → data/transcripts/   [gitignored]
   ▼
parse       ── front-matter + body; derive title, guest, episode URL
   ▼
hash        ── sha256(body). Unchanged hash → skip episode entirely.
   ▼
chunk       ── speaker-aware, ~750 tokens target, ~120 token overlap
   ▼
embed       ── batched; cached by chunk content_hash
   ▼
upsert      ── episodes + chunks, transactional per episode
```

**Why the corpus is not committed to this repo.** It is third-party content. Vendoring
hundreds of transcripts into a public repo redistributes someone else's material and bloats
the clone. `make ingest` fetches it at setup time from the public upstream. `TRANSCRIPT_REPO`
points the fetcher elsewhere if the client's corpus differs (assumption A1).

**Refresh.** `make ingest` is idempotent and incremental — re-running it only re-embeds
episodes whose `content_hash` changed. A weekly cron is sufficient (assumption A3).

### 4.2 Chunking — speaker-aware, not fixed-size

Transcripts are dialogue, not prose (assumption A8). A naive fixed-size splitter cuts
mid-answer and produces chunks that begin with a pronoun whose referent is in the previous
chunk — which embeds poorly and reads worse when shown as a citation.

The chunker instead:

1. Segments on speaker-turn boundaries first.
2. Packs consecutive turns up to a ~750-token target, never splitting a short turn.
3. Splits a single over-long turn on sentence boundaries, with ~120-token overlap.
4. Prefixes each chunk with a compact `Episode | Guest` header so the embedding carries
   topical context even when the chunk body is a bare pronoun-heavy reply.
5. Records `char_start`/`char_end` so a citation can be traced to an exact span of the
   source file.

Step 4 is small and matters more than it looks: it is what lets a chunk that literally
reads "yeah, we did that for about six months" still retrieve for a query about the topic
being discussed.

### 4.3 Retrieval — hybrid, fused with RRF

Two retrievers run concurrently, then fuse:

- **Dense:** cosine similarity over `nomic-embed-text` embeddings via pgvector HNSW.
  Good at paraphrase and concept matching.
- **Lexical:** PostgreSQL full-text search over the generated `tsv` column.
  Good at proper nouns, product names, and jargon — exactly where dense retrieval is weakest.

Fusion is **Reciprocal Rank Fusion**, `score = Σ 1/(k + rank_i)` with `k=60`. RRF is
rank-based, so it needs no score normalization between two retrievers whose scores are not
on a comparable scale. That property is why it is used here rather than a weighted sum,
which would require tuning a weight per corpus.

**The relevance floor.** Fused candidates below `RAG_MIN_SCORE` are discarded. If nothing
survives, the retriever returns empty and the orchestrator **short-circuits without calling
the model at all** (PRD R1, mitigation 2). This is the single most important line of defense
against confident fabrication, and it is a control-flow guarantee rather than a prompt
instruction.

**Diversity cap.** At most `RAG_MAX_PER_EPISODE` chunks from any one episode, so an answer
does not silently become a summary of a single interview when the corpus has broader coverage.

---

## 5. Agent layer and routing

### 5.1 Why a provider-agnostic runtime

The brief asks for the **Claude Agent SDK** *and* mandates that the demo run on **local
Ollama**. These pull in opposite directions: the Agent SDK targets Anthropic models and
does not drive an Ollama backend.

**Resolution.** The agent runtime in `app/agent` implements the Agent-SDK *contracts* —
filesystem-defined skills with a `SKILL.md`, explicit tool boundaries, a system-prompt
composition step, and a permission-gated tool surface — over a provider abstraction that
any of the three providers satisfies. When `ANTHROPIC_API_KEY` is present, the
`claude_sdk` executor delegates to the real `claude-agent-sdk` package.

This is a deliberate trade-off, and it is the one I would most expect to be challenged on:

- **Gained:** the mandatory local demo actually works, skills are unit-testable without a
  network, and provider swap is genuinely config-only.
- **Given up:** the SDK's built-in session management and MCP tool ecosystem, which we
  reimplement narrowly for our three skills.
- **Rejected alternative:** running the Agent SDK against an Ollama-to-Anthropic
  translation proxy. It technically satisfies both requirements, but it puts an
  unsupported shim on the critical path of the demo, and an evaluator debugging a failure
  would be debugging the shim rather than the product.

### 5.2 Router

Three intents: `grounded_qa` (default), `ship30_essay`, `artifact`.

Routing is **deterministic-first**:

1. **Explicit signal** — an API-level `skill` override or a UI action button. Always wins.
2. **High-precision patterns** — verb/noun pairs ("write an essay", "make me a one-pager",
   "as HTML"). Tuned for precision over recall; an ambiguous phrase falls through.
3. **LLM classification** — only for what survives, constrained to a JSON schema with the
   valid intents enumerated.
4. **Default** — `grounded_qa`.

The ordering is the point. Routing on a 3B CPU model is the least reliable step in the
system (PRD R3), so the design keeps the common cases off that path entirely. Every routing
decision is logged with its deciding stage, so misroutes are diagnosable rather than mysterious.

### 5.3 Skills

Each skill is a directory with a `SKILL.md` (the encoded methodology, editable without
code) and a Python module (the execution mechanics).

| Skill | Input | Output | Grounding |
|---|---|---|---|
| `grounded_qa` | question + session context | streamed prose with `[S#]` citations | retrieval-constrained; abstains on empty retrieval |
| `ship30_essay` | topic, optionally a prior answer | ~1,250-word essay, auto-promoted to a Markdown artifact | retrieves its own evidence; claims cited |
| `artifact` | request + conversation context | sanitized `markdown` or `html` artifact | grounded when the content is factual |

**Ship 30 for 30 is drafted section-wise**, not in one 1,250-word generation. A 3B model
loses structural coherence over long outputs; asking for an outline, then each section
against that outline, keeps every individual generation inside the model's competent range.
Word-count targets are enforced per section and the essay is re-balanced if a section
overruns.

### 5.4 Citation validation

After generation, every `[S#]` marker is matched against the source list actually passed to
the model. Markers that do not resolve are stripped from the output and counted in a
structured log line. An answer that ends with zero valid citations is flagged rather than
silently returned — this is the mechanism behind PRD acceptance criterion AC5.

---

## 6. API contracts

Base path `/api`. All errors share one envelope; validation is Pydantic v2 throughout.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness. Never touches dependencies. Always fast. |
| `GET` | `/api/health/ready` | Readiness. Per-dependency status: db, ollama, providers, corpus, embedding-model agreement. |
| `GET` | `/api/config` | Available providers, active provider/model, corpus stats, feature flags. Drives the UI. |
| `POST` | `/api/sessions` | Create a session. |
| `GET` | `/api/sessions` | List sessions (paginated). |
| `GET` | `/api/sessions/{id}` | Session detail with full message history. |
| `PATCH` | `/api/sessions/{id}` | Rename, or change provider/model. |
| `DELETE` | `/api/sessions/{id}` | Soft delete. |
| `POST` | `/api/sessions/{id}/messages` | Send a message; buffered JSON response. |
| `POST` | `/api/sessions/{id}/stream` | Send a message; SSE stream. Primary UI path. |
| `GET` | `/api/sessions/{id}/artifacts` | Artifacts produced in a session. |
| `GET` | `/api/artifacts/{id}` | Fetch one artifact (sanitized content). |
| `POST` | `/api/search` | Raw retrieval, no generation. Debugging + evaluation. |
| `GET` | `/api/corpus/stats` | Episode/chunk counts, last ingestion, embedding model. |

### 6.1 Error envelope

Every non-2xx response, including framework validation failures, is normalized to:

```json
{
  "error": {
    "code": "PROVIDER_UNAVAILABLE",
    "message": "Ollama is not reachable at http://localhost:11434.",
    "detail": {"provider": "ollama", "attempted": ["ollama"]},
    "hint": "Start Ollama with `ollama serve`, or switch provider in the header.",
    "correlation_id": "0b41…"
  }
}
```

The `hint` field is deliberate. A structured error that tells an operator *what to do next*
is the difference between a support ticket and a self-service fix, and it costs one string.

### 6.2 SSE event protocol

`POST /api/sessions/{id}/stream` emits typed events so the UI can render progress
truthfully rather than showing an undifferentiated spinner:

| Event | Payload | UI effect |
|---|---|---|
| `routing` | `{intent, stage}` | "Choosing approach…" |
| `retrieval` | `{count, took_ms, sources[]}` | Renders the source chips *before* text arrives |
| `token` | `{text}` | Appends to the streaming bubble |
| `artifact_open` | `{id, kind, title}` | Opens the viewer pane early |
| `artifact_chunk` | `{text}` | Streams into the viewer |
| `citations` | `{citations[]}` | Finalizes the source panel |
| `done` | `{message_id, usage, latency_ms}` | Settles the turn |
| `error` | error envelope | Inline, retryable error state |

Emitting `retrieval` before the first `token` is a UX decision with a grounding purpose:
the user sees which sources are being used *while* the answer is still generating, which
makes the citation contract feel real rather than decorative.

---

## 7. Model configuration and toggle

### 7.1 Configuration layer

All model selection is environment-driven; no code change switches a provider.

```
LLM_PROVIDER=ollama                   # ollama | anthropic | openai
LLM_FALLBACK_ORDER=ollama,anthropic   # ordered chain, comma-separated
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
ANTHROPIC_API_KEY=                    # optional
ANTHROPIC_MODEL=claude-sonnet-4-5
OPENAI_API_KEY=                       # optional
OPENAI_MODEL=gpt-4o-mini
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=768
```

Every provider implements one `ChatProvider` protocol (`complete`, `stream`, `health`).
The registry resolves the active provider per session, so two concurrent sessions can run
different providers.

### 7.2 Availability and fallback

A provider is **available** only if its configuration is complete *and* its health check
passes. The UI shows unavailable providers disabled **with the reason attached** rather
than hiding them — a missing option generates a support ticket; a disabled option with
"no API key configured" does not.

**Documented fallback behavior:**

1. Try the session's provider.
2. On a *transport* failure (connection refused, timeout, 5xx), walk `LLM_FALLBACK_ORDER`,
   skipping unavailable providers.
3. On a *semantic* failure (bad request, context-length exceeded), do **not** fall back —
   another provider will fail identically. Return a structured error with a hint.
4. If every provider fails, return `503 PROVIDER_UNAVAILABLE` listing what was attempted.
5. Any fallback is surfaced in the UI ("answered by anthropic — ollama was unreachable").

Point 3 is the one that matters. Blind retry across providers on a semantic error burns
latency and money to reproduce the same failure, and hides the actual bug.

**Timeouts.** Every provider call is bounded by `LLM_TIMEOUT_SECONDS` (default 120, sized
for CPU inference). Streaming calls additionally enforce an inter-token stall timeout, so a
model that connects and then hangs fails in seconds rather than at the full request timeout.

---

## 8. Artifact security

Generated HTML is treated as **untrusted**, always. Not because the model is adversarial,
but because a prompt-injected transcript can steer any model, and because "the trusted
model produced it" is not a property the renderer can verify.

### 8.1 Four layers

**Layer 1 — Server-side sanitization (`app/security/sanitize.py`).**
Allowlist, not denylist. Permitted: structural and text elements, tables, lists, `style`
blocks, `class`/`id`/`style` attributes, `img` with `data:` URIs. Removed: `<script>`,
`<iframe>`, `<object>`, `<embed>`, `<base>`, `<link>`, `<meta http-equiv>`, `<form>`, all
`on*` event-handler attributes, and any `javascript:`/`vbscript:`/`data:text/html` URL.
Every removal is recorded in `sanitization_report`.

**Layer 2 — CSP injection.**
The sanitized document is wrapped with a generated meta CSP:

```
default-src 'none'; style-src 'unsafe-inline'; img-src data:;
font-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'none'
```

`default-src 'none'` is the load-bearing directive: it blocks **all** network egress —
`fetch`, `XHR`, WebSocket, remote images, remote fonts. An artifact cannot exfiltrate
conversation content even if it somehow executes.

**Layer 3 — iframe sandbox.**
Rendered as `<iframe sandbox="allow-scripts">`. Critically, **never**
`allow-scripts allow-same-origin` together — that specific pair lets framed script reach
back into the parent origin and defeats the sandbox entirely. It is the single most common
way artifact viewers are built insecurely, and there is a test asserting the attribute
string never contains both.

**Layer 4 — `srcdoc` into an opaque origin.**
The document is delivered via `srcdoc`, so the frame has a null origin with no access to
app cookies, `localStorage`, `sessionStorage`, or the parent DOM. Navigation is not
permitted (`allow-top-navigation` is absent), so an artifact cannot redirect the app away.

### 8.2 What the viewer permits and blocks

| Capability | Status | Why |
|---|---|---|
| HTML structure, CSS, layout, animation | **Permitted** | The point of the feature |
| Inline `<style>`, scoped classes | **Permitted** | Needed for real formatting |
| Images as `data:` URIs | **Permitted** | Self-contained, no egress |
| Inline JS (charts, interactivity) | **Permitted, contained** | Runs in an opaque origin with no network and no parent access |
| Remote scripts, styles, fonts, images | **Blocked** | Network egress = exfiltration channel |
| `fetch` / `XHR` / WebSocket | **Blocked** (CSP) | Same |
| Access to parent DOM, cookies, storage | **Blocked** (sandbox + opaque origin) | Session hijacking |
| Form submission | **Blocked** (`form-action 'none'`) | Credential phishing vector |
| Top-level navigation, popups | **Blocked** (sandbox) | Clickjacking / redirect |

Blocked items are reported to the user in the viewer ("2 elements removed — view report"),
so removal is *visible* rather than silent. Silent stripping produces bug reports about
artifacts that "just don't work"; a visible report turns that into an understood constraint.

**Markdown artifacts** take the same path: rendered to HTML, then run through the identical
sanitizer. Markdown permits raw inline HTML, so treating it as inherently safe would be a
mistake.

---

## 9. Deployment topology

### 9.1 Local (the delivered configuration)

```
docker compose up
   ├── db        postgres 16 + pgvector    :5432   healthcheck: pg_isready
   ├── backend   uvicorn / FastAPI          :8000   healthcheck: /api/health
   │               └─ host.docker.internal:11434 → Ollama on the host
   └── frontend  nginx serving Vite build   :5173   healthcheck: /
```

**Ollama runs on the host, not in a container.** In a container it would lose GPU access
where a GPU exists, and would duplicate a multi-gigabyte model store. The backend reaches
it via `host.docker.internal`, which Compose maps explicitly for Linux hosts too.

Reference machine for all latency figures in the PRD: 16 GB RAM, 12-core mobile CPU,
integrated graphics, **CPU-only inference**.

### 9.2 Cloud path (sketched, not built)

Out of scope per the brief, recorded so the client is not guessing: managed Postgres with
pgvector (Supabase or RDS), backend as a container on Railway/Fly/Cloud Run, frontend on
any static host, and `LLM_PROVIDER=anthropic` — because a cloud deployment has no local
Ollama, and this is precisely the swap the configuration layer exists to make trivial.
The only genuinely new work is secret management and network policy.

---

## 10. Operability

**Structured logging.** JSON to stdout. Every request gets a `correlation_id` propagated
through routing, retrieval, provider calls, and persistence, so one grep reconstructs a
full turn. Secrets are redacted by key name at the formatter, not at call sites — the
formatter cannot be forgotten.

Each layer emits one purposeful event: `retrieval.completed` (query, candidates, kept,
scores, ms), `llm.completed` (provider, model, tokens, ms, fallback_used),
`artifact.sanitized` (removals by category), `router.decided` (intent, deciding stage).
These four cover the four failure domains the brief names — model, retrieval, database,
artifact rendering.

**Resilience matrix.**

| Failure | Behavior |
|---|---|
| No API key for selected provider | Provider marked unavailable at startup; UI disables it with the reason; falls back per chain |
| Ollama unreachable | Readiness reports it; fallback chain walked; actionable error with a start command |
| Model timeout / inter-token stall | Bounded, aborted, structured error, partial output preserved |
| Empty retrieval | Model never invoked; structured "not covered in the corpus" response |
| Database unavailable | Readiness fails; connection pool retries with backoff; chat returns a clear error rather than a stack trace |
| Corpus not yet ingested | Readiness reports `corpus: empty` with the `make ingest` command as the hint |
| Embedding-model mismatch | Retrieval refuses to serve and names both models — never returns incomparable neighbours |

**Known limitations, stated plainly.** Schema is applied idempotently rather than via
versioned migrations — correct for one environment, insufficient for two. Artifact
regeneration overwrites rather than versioning. There is no auth. Claim-level entailment
checking is not implemented, so the residual misattribution risk in PRD R1 stands.
