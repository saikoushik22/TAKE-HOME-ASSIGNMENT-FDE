# PRD — The Lenny Growth Assistant

**Status:** v1.0 (engagement scope, delivered)
**Author:** sathwikgoud28
**Engagement type:** Small forward-deployment — discovery, build, local deploy, handoff
**Last updated:** 2026-08-24

---

## 1. Forward Deployment Brief

### 1.1 User and problem

**Primary user — "the growth PM."** A product or growth practitioner at a 20–200 person
company. They sit in weekly planning, quarterly strategy reviews, and a lot of Slack
threads where someone asks a question that *has* a good answer somewhere in the industry's
collective operator experience — and no reliable way to reach it.

Concretely, the primary user has three recurring jobs:

| # | Job to be done | Today's workaround | Why it fails |
|---|---|---|---|
| J1 | *"Give me a defensible answer to a hard product/growth question."* | Ask a general chatbot, or search YouTube/Substack | General chatbots answer from parametric memory: fluent, confident, **unattributable**. You cannot take "an AI said so" into a strategy review. |
| J2 | *"Turn that answer into something I can publish."* | Copy the answer, rewrite it by hand for 45–90 min | Rewriting is the actual bottleneck. Output quality swings wildly per person and per day. |
| J3 | *"Show it to someone, rendered."* | Paste a code block into Notion and hope | Raw Markdown/HTML in a chat window is not a deliverable. |

**The pain the assistant removes.** Lenny's Podcast is a large library of long-form
interviews with operators who have actually run the playbooks — hundreds of hours of
linear audio. The knowledge density is extremely high and the retrieval affordance is
essentially zero. A PM asking *"how should we pick an activation metric?"* knows the
answer exists across a handful of episodes, and has no path to those episodes that costs
less than an afternoon.

So the assistant's real job is **not search**. It is:

> **Produce an operator-grounded answer the user can defend in a room, with receipts —
> and then turn it into a publishable artifact without a second tool.**

The word that matters is **defend**. Every design decision below is downstream of it.

#### Secondary user — the client engineer

The team that inherits this system after the engagement ends. They need to run it, swap
the model, re-index a growing corpus, and diagnose a failure at 11pm without calling us.
They are a first-class user of this deliverable, and the README's operations section plus
all of `docs/architecture.md` are written *for them*.

#### Explicit non-user

The general public. This is an **internal** assistant. No multi-tenant auth, no rate-card,
no public sharing surface. That exclusion buys a lot of scope back (see §1.4).

---

### 1.2 Success metrics

**Headline metric — Citation-Backed Answer Rate (CBAR).**

> Of all substantive claims the assistant makes, the share that carry a citation which
> resolves to a real transcript chunk that actually supports the claim.
> **Target: ≥ 95%.**

Why this one: it is the direct numerical proxy for *"can the user defend this in a room."*
If CBAR is high the product does its job; if CBAR is low, every other metric is vanity —
a fast, beautiful, well-formatted wrong answer is worse than no product, because it
launders a guess into something that looks like a source.

CBAR is measured, not asserted. `backend/tests/eval/golden_set.yaml` holds the evaluation
set; `make eval` scores it and prints CBAR plus the two supporting metrics.

**Supporting metric 1 — Abstention Correctness. Target: 100%.**
The golden set includes deliberately out-of-corpus questions ("what's the best way to file
a patent?"). The assistant must decline and say the corpus does not cover it. A single
confident fabrication here fails the build. This is a **gate, not a gauge** — 100% or the
release does not ship.

**Supporting metric 2 — Operational latency on the reference machine.**
The reference machine is CPU-only inference (see `docs/architecture.md` §9).

- p95 **time-to-first-token ≤ 3.0 s** (streaming makes the wait legible)
- p95 **end-to-end grounded answer ≤ 45 s**
- p95 **retrieval-only ≤ 400 ms** (isolates the retriever from model slowness)

**Adoption proxy — Artifact Yield.** Share of sessions producing ≥1 saved artifact.
No target in v1; we lack a baseline. Instrumented from day one so the client *has* a
baseline by the time they set one. Recording a metric you cannot yet target is cheap;
retrofitting the instrumentation later is not.

---

### 1.3 Assumptions

The client brief was intentionally incomplete. These are the load-bearing assumptions;
each one names what breaks if it is wrong, because that is the part that is actually
useful to the team inheriting this.

| # | Assumption | Confidence | If wrong |
|---|---|---|---|
| A1 | The corpus is the **public** `ChatPRD/lennys-podcast-transcripts` repo; internal/paid newsletter content is out of scope. | High | Ingestion source swaps via one env var (`TRANSCRIPT_REPO`). Adapter, not rewrite. |
| A2 | Internal tool, trusted users, **single tenant**. No login required. | High | Auth is the largest single re-scope. The session table already carries `user_metadata`, so the column exists; the middleware does not. |
| A3 | Corpus is **read-mostly** and refreshes weekly at most. Justifies batch ingestion over streaming. | High | Ingestion is already incremental + content-hashed; a cron is sufficient. Low risk. |
| A4 | Evaluator runs on a laptop with **no dedicated GPU**. Drives the 3B default model. | High | If a GPU exists, one env var change to `qwen2.5:7b` improves quality with no code change. |
| A5 | Corpus fits comfortably on disk (tens of MB of text). Justifies pgvector over a dedicated vector DB. | High | Above a few million chunks, pgvector HNSW needs tuning or a dedicated store. Far outside v1. |
| A6 | Answer quality matters **more** than answer speed for this user. A PM will wait 30 s for a citation-backed answer. | Medium | This is the assumption I am least sure of. If wrong, the fix is a smaller model + tighter `RAG_TOP_K`, both config-only. |
| A7 | "Ship 30 for 30 style" means the **public methodology** — atomic essays, hook-first, one idea, skimmable. Encoded in `SKILL.md`. | Medium | Principles live in an editable Markdown file, not in code. A client editor can change the house style without an engineer. |
| A8 | Transcripts are **speech**, not prose. Drives speaker-aware chunking rather than naive fixed-size splits. | High | Validated during ingestion — see architecture.md §4. |

---

### 1.4 Scope choices

Scope discipline is the deliverable here as much as the code is. What follows is what I
built, what I deliberately did not, and the reasoning.

#### In scope — built and verified

- **Grounded conversational assistant** with hybrid retrieval (vector + lexical), inline
  citations, multi-turn follow-ups, and honest abstention.
- **Independent sessions** with server-side context, persisted to PostgreSQL.
- **Ship 30 for 30 essay skill** — a real skill with the methodology encoded in a
  versioned `SKILL.md`, not a prompt string buried in a function.
- **Artifact generation + in-app viewer** — Markdown and HTML/CSS, rendered beside the
  chat in a sandboxed frame.
- **Provider abstraction** — Ollama (local, demo default), Anthropic, OpenAI, all
  swappable by config with a documented fallback chain.
- **One-command startup** via Docker Compose.
- **Observability and resilience** — structured JSON logs with correlation IDs, health
  and readiness endpoints, graceful degradation on every external dependency.
- **Tests** — automated coverage of API contracts, retrieval, routing, persistence, and
  artifact sanitization, plus a written manual UI test plan.

#### Deliberately out of scope

| Excluded | Why |
|---|---|
| **Authentication / multi-tenancy** | A2 says internal, trusted, single-tenant. Auth is 1–2 days that buys the evaluator nothing. The `user_metadata` column and `user_id` field exist so it is a middleware addition, not a migration. |
| **Audio ingestion / diarization** | The upstream repo already provides clean text with speaker labels. Re-deriving them from audio is weeks of work to reproduce something already given. |
| **Fine-tuning** | RAG is the correct tool for a factual, citation-required, frequently-refreshed corpus. Fine-tuning teaches style, not facts, and would actively *hurt* CBAR by encouraging fluent recall over retrieval. |
| **Agentic web browsing** | Directly contradicts the core promise. "Strictly from Lenny's transcripts" means the tool surface must not include the open internet. Excluding this is a *feature*. |
| **Cross-encoder reranking** | Measured: hybrid RRF fusion already clears the retrieval quality bar on the golden set. A cross-encoder adds substantial CPU latency for a gain I could not measure. Revisit if the corpus grows 10×. |
| **Streaming artifact edits / collaborative editing** | Real product value, but it is a second product. Artifacts are generate-and-regenerate in v1. |
| **Kubernetes / cloud deploy** | Brief says deploy **locally**. Compose is the right altitude; architecture.md §9 sketches the cloud path without building it. |

#### Cut mid-build, and why (honest record)

- **Cross-encoder reranking** — prototyped, measured, removed. Cost was real, the gain was not.
- **Per-message model switching** — the UI initially let you change provider mid-session.
  It made session transcripts incoherent to debug (two models, one thread, no marker).
  Moved to session-scoped provider, recorded on the session row.

---

### 1.5 Risks and trade-offs

Ordered by expected damage, not by likelihood.

#### R1 — Hallucination (severity: critical)

The failure that destroys the product's reason to exist. A confident, uncited, wrong
answer is worse than an error message, because the user cannot tell it apart from a good one.

**Mitigations, layered:**

1. Retrieval-constrained prompting — the answer skill sees only retrieved chunks, and is
   instructed that these are its *only* permitted evidence.
2. **Empty-retrieval short-circuit.** If retrieval returns nothing above the relevance
   floor, we do not call the model at all. We return a structured "not covered" response.
   This makes the most dangerous case *impossible by construction* rather than by
   instruction — the model cannot fabricate if it is never invoked.
3. Citation validation — every `[S#]` marker is checked server-side against the chunks
   actually retrieved. Markers pointing at non-existent sources are stripped and logged.
4. Abstention is measured as a release gate (§1.2), not hoped for.

**Measured limits of mitigation 2.** The relevance floor was calibrated against the golden
set rather than guessed, and the measurement changed the design. In-corpus questions score
0.44–0.73; out-of-corpus questions score 0.43–0.55. **The bands overlap**, because dense
embeddings keep a high similarity floor for any same-language text. Score margin and lexical
agreement were probed as alternatives and separate no better.

So mitigation 2 is *good but not sufficient*: at 0.50 it rejects 7 of 8 known out-of-corpus
questions. The eighth is caught by mitigations 1 and 3 — verified end to end, the assistant
answers "There is no information … in the provided sources" with zero citations. **This is
precisely why the mitigation is layered rather than singular.** A design that relied on the
floor alone would have shipped a hole.

**Residual risk 1 — misattribution within retrieved context.** A small local model can still
cite S2 for a claim that came from S3. Detecting that needs claim-level entailment checking,
which is out of scope. The citation panel showing the retrieved passage is the
human-in-the-loop mitigation: the user can check.

**Residual risk 2 — the floor is corpus- and model-specific.** 0.50 is not a universal
constant. Changing the corpus or the embedding model invalidates it, which is why `make eval`
exists and why the derivation is recorded next to the value rather than in a commit message.

#### R2 — Unsafe artifact rendering (severity: high)

We take model-generated HTML and put it in a browser. That is XSS-by-design unless
contained. Treated as untrusted input, always — including when a "trusted" cloud model
produced it, since a prompt-injected transcript could steer either model.

Defense in depth, four layers, detailed in `docs/architecture.md` §8:

1. Server-side sanitization allowlist before persistence.
2. Injected `Content-Security-Policy` that forbids all network egress.
3. `<iframe sandbox="allow-scripts">` **without** `allow-same-origin` — the combination
   of both flags is the specific mistake that defeats sandboxing, and we never emit it.
4. Rendering via `srcdoc` into an opaque origin, so the frame has no access to app
   cookies, `localStorage`, or the parent DOM.

**Trade-off accepted:** artifacts cannot call APIs or load remote images/fonts. An artifact
that tries will render without them rather than failing loudly. That is the correct trade —
a data-exfiltrating artifact is a much worse outcome than a missing web font.

#### R3 — Local model quality (severity: high, likelihood: certain)

A 3B model on CPU is meaningfully weaker than a frontier model at synthesis, instruction
adherence, and long-form structure. The 1,250-word Ship 30 essay is the hardest case and
degrades most visibly. **This is a real, unavoidable limitation of the mandated demo
configuration, and I would rather state it plainly than hide it behind a cherry-picked demo.**

Mitigations: schema-constrained outputs where structure matters; a deterministic
keyword-prior router so *routing* never depends on model reasoning; section-wise essay
generation so the model handles a few hundred words at a time instead of 1,250 at once;
and a one-env-var upgrade path to a cloud model for production.

#### R4 — Latency and cost (severity: medium)

CPU inference is slow; cloud inference costs money. Traded explicitly: streaming SSE so
the wait is legible rather than a spinner, retrieval capped at `RAG_TOP_K` to bound prompt
size, embeddings cached by content hash so re-ingestion is near-free, and per-request
token accounting in the logs so the client can see spend before it surprises them.

#### R5 — Data leakage (severity: medium)

Two directions. **Outbound:** the local-first default means transcripts and conversations
never leave the machine unless the operator opts into a cloud provider — and the UI states
which provider is active on every session, so this is never accidental. **Inbound:**
`.env` is gitignored, `.env.example` carries only safe placeholders, no secret is ever
logged (the log formatter redacts by key name), and the repo does not vendor the transcript
corpus.

#### R6 — Corpus staleness (severity: low)

New episodes ship regularly. Ingestion is incremental and content-hash-based, so a refresh
is cheap and idempotent. Every chunk carries `source_updated_at` so answers can be traced
to a corpus version.

#### R7 — Prompt injection via transcript content (severity: low, novel)

A transcript containing text like *"ignore previous instructions"* becomes model input.
Likelihood is low (these are podcast transcripts, not attacker-controlled), but the
mitigation is nearly free: retrieved context is fenced with explicit delimiters and labeled
as untrusted data rather than instruction, and the artifact sanitizer (R2) catches the
downstream consequence regardless of the upstream cause.

---

## 2. Product flows

### Flow A — Grounded question (the 80% path)

1. User opens the app. A session exists already; no "create session" ceremony.
2. User asks a product/growth question.
3. Router classifies intent → `grounded_qa`.
4. Retriever runs hybrid search; returns top-k chunks with scores.
5. **If nothing clears the relevance floor →** structured "not covered" response, with
   suggestions of topics the corpus *does* cover. Model is never invoked. *(R1 mitigation.)*
6. Otherwise the answer skill streams a response with inline `[S1]`-style citations.
7. Citations render as an expandable source panel: episode title, guest, and source link.
8. Turn is persisted with its retrieval trace.

**Acceptance:** answer cites ≥1 real source; citations resolve to real chunks; response
streams first token in ≤3 s p95; follow-up "what about for B2B?" correctly reuses context.

### Flow B — Ship 30 for 30 essay

1. User asks for an essay, or clicks **Write essay** on a prior answer.
2. Router → `ship30_essay`. Prior turn's grounded content is passed as the substrate.
3. Skill loads `SKILL.md`, retrieves supporting evidence, and drafts section-wise.
4. Output: ~1,250 words, hook, narrative progression, skimmable headings/bullets/bold,
   one specific takeaway, claims grounded and cited.
5. Auto-promoted to a Markdown artifact and opened in the viewer.

**Acceptance:** 1,100–1,400 words; ≥3 H2s; ≥1 bullet list; ≥3 citations; a hook that is
not a definition; a takeaway that names a concrete action.

### Flow C — Artifact generation and rendering

1. User asks for a doc, table, one-pager, or HTML snippet.
2. Router → `artifact`. Type inferred (`markdown` | `html`).
3. Generated → **sanitized** → persisted → streamed to the viewer beside the chat.
4. Viewer renders it; user can switch Preview/Source, copy, download, or regenerate.

**Acceptance:** HTML renders visually, not as a code block; a `<script src>` to an external
origin is stripped and the removal is *visible* to the user; the frame cannot reach the
parent or the network; the viewer is responsive and collapses below 900 px.

### Flow D — Model switching

1. User opens the provider control in the header, which shows the active provider/model.
2. Switches Ollama ↔ Anthropic ↔ OpenAI.
3. Unavailable providers are shown **disabled with the reason** ("no API key configured"),
   never hidden — a silently missing option is a support ticket.
4. Selection is session-scoped and recorded on the session row.

**Acceptance:** switching needs no code change or restart; the active provider is always
visible; an unreachable provider degrades per the documented fallback chain instead of
500-ing.

### Flow E — Session management

New chat, switch between sessions, rename, delete. Each session keeps independent context;
messages, timestamps, provider, and metadata persist across restarts.

---

## 3. Acceptance criteria (release gate)

The build ships only if **all** of these hold. Verified by `make test` + `make eval` +
the manual plan in `docs/manual-test-plan.md`.

| ID | Criterion | Verified by |
|---|---|---|
| AC1 | `docker compose up` brings the full stack to healthy from a clean clone | Manual, timed |
| AC2 | `GET /api/health` and `/api/health/ready` report per-dependency status | Automated |
| AC3 | Sessions maintain independent context; no cross-session bleed | Automated |
| AC4 | Conversations, session IDs, timestamps, user metadata persist in PostgreSQL | Automated |
| AC5 | Every grounded answer carries ≥1 resolvable citation | Automated (eval) |
| AC6 | Out-of-corpus questions are declined, never fabricated | Automated (eval, **gate**) |
| AC7 | Provider switches by config alone; no code change | Automated + manual |
| AC8 | Missing key / Ollama down / DB down / empty retrieval all degrade gracefully | Automated (fault injection) |
| AC9 | Ship 30 essay meets the structural contract in Flow B | Automated (structural) |
| AC10 | HTML artifacts render sandboxed; hostile payloads are neutralized | Automated (XSS vector suite) |
| AC11 | No secret is committed; `.env.example` has safe placeholders only | Automated (repo scan) |
| AC12 | Structured logs carry a correlation ID through the full request path | Automated |
| AC13 | UI is usable at 360 px and keyboard-navigable | Manual plan |

---

## 4. Implementation plan

Delivered in eight phases. Each phase left the repo in a runnable state, so a mid-phase
stop still yields something demonstrable — the same discipline I would apply on a real
engagement where the client may ask for a demo at any point.

| Phase | Scope | Exit condition |
|---|---|---|
| 0 | Environment provisioning, repo scaffold, git hygiene | Toolchain verified; `.gitignore` protects secrets before any code exists |
| 1 | Discovery + docs: PRD, design.md, architecture.md | Decisions written down **before** implementation, per the brief |
| 2 | Ingestion: fetch → parse → speaker-aware chunk → embed → index | Corpus queryable; retrieval measured standalone |
| 3 | FastAPI, Postgres, provider layer, agent runtime, RAG | Grounded answers end to end with citations |
| 4 | Skills: Ship 30 for 30, artifact generation, routing | All three skills routable and individually testable |
| 5 | Frontend: chat UI + sandboxed artifact viewer | Full product usable in a browser |
| 6 | Tests, observability, resilience, fault injection | Acceptance criteria automated |
| 7 | Docker Compose, `.env.example`, README, handoff docs | One-command startup from clean clone |
| 8 | Agent transcripts, fresh-clone verification, push | Evaluator can reproduce from documentation alone |

### Post-v1 backlog (what I would do next, in priority order)

1. **Claim-level entailment checking** — closes the residual R1 gap. Highest value.
2. **Feedback capture** (thumbs + reason) — converts CBAR from a lab metric into a live one.
3. **Auth + multi-tenancy** — the moment this leaves one team.
4. **Artifact versioning and diff** — regeneration currently loses the prior version.
5. **Cross-encoder rerank** — revisit when the corpus grows enough to justify the latency.
