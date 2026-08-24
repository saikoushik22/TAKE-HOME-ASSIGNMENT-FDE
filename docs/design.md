# Design — The Lenny Growth Assistant

**Audience:** anyone extending the UI, and the evaluator judging UI/UX quality.
**Companion docs:** `docs/PRD.md` (user + flows), `docs/architecture.md` (contracts).

---

## 1. Design principles

Five principles, in priority order. When two conflict, the higher one wins — that ordering
is the actual content here, since every UI has a moment where "beautiful" and "honest"
disagree.

### P1 — Receipts are the product, not a footnote

The PRD's core promise is an answer the user can **defend**. So citations are not a
collapsed "sources" accordion at the bottom that nobody opens. They are:

- **inline**, as clickable `[1]` markers in the sentence that makes the claim,
- **early**, as source chips that appear *before* the first token of prose arrives,
- **inspectable**, expanding to the retrieved passage with episode, guest, and link.

A user who never opens a citation still benefits: seeing them appear is what makes the
answer feel accountable. A user who does open one gets the receipt. Both are served by
showing sources during generation rather than after it.

### P2 — Show the machine's state, never a mystery spinner

A CPU-only local model can take 30+ seconds. An undifferentiated spinner for 30 seconds
reads as broken. The same 30 seconds narrated — *choosing approach → searching 12 sources →
writing* — reads as working.

This is why the SSE protocol emits typed events (`architecture.md` §6.2). The design
requirement drove the protocol design, not the other way around.

### P3 — Degrade honestly

Every failure state names what broke and what to do next. No `[object Object]`, no silent
empty state, no generic "Something went wrong."

The strongest version of this: when the corpus does not support a question, the assistant
says so **as a designed state** with suggested in-corpus topics — not as an error, and not
as a hedged non-answer. Abstention is a first-class UI state because it is a first-class
product behavior.

### P4 — The artifact is a workspace, not an attachment

Artifacts render **beside** the chat, not inside a message bubble. The chat stays
navigable while a document is open. Preview/Source is one toggle, not a modal round-trip.

### P5 — Boring where it counts

Standard chat affordances behave exactly as expected: Enter sends, Shift+Enter newlines,
the transcript auto-scrolls unless you have scrolled up, `Esc` closes panes. Novelty is
spent on the artifact viewer and the grounding surface. Nowhere else.

---

## 2. Information architecture

```
┌─────────────┬──────────────────────────────────┬────────────────────────┐
│  Sidebar    │  Conversation                    │  Artifact Viewer       │
│  (260px)    │  (fluid, max 760px measure)      │  (fluid, 40–55%)       │
│             │                                  │                        │
│ + New chat  │  ┌────────────────────────────┐  │  ┌──────────────────┐  │
│             │  │ Header: title · provider   │  │  │ Title  [Preview] │  │
│ Today       │  └────────────────────────────┘  │  │        [Source ] │  │
│  · Activa…  │                                  │  ├──────────────────┤  │
│  · Pricing… │   [user bubble]                  │  │                  │  │
│             │                                  │  │  sandboxed       │  │
│ Earlier     │   [assistant]                    │  │  iframe          │  │
│  · Retenti… │    ├ source chips (S1 S2 S3)     │  │                  │  │
│             │    ├ prose with [1] markers      │  │                  │  │
│             │    └ actions: copy · essay · art │  │                  │  │
│             │                                  │  ├──────────────────┤  │
│             │  ┌────────────────────────────┐  │  │ ⚠ 2 removed      │  │
│             │  │ composer                   │  │  │ copy · download  │  │
│             │  └────────────────────────────┘  │  └──────────────────┘  │
└─────────────┴──────────────────────────────────┴────────────────────────┘
```

**Three regions, one job each.** Sidebar = *which* conversation. Center = *the*
conversation. Right = *the output of* the conversation. The artifact pane is absent until
an artifact exists, so the default state is a clean two-column chat rather than an empty
third of the screen.

**Persistent global state** lives in the conversation header, not the sidebar: session
title and the active provider badge. The provider is shown on every screen because
"which model answered this?" is a question the user must never have to hunt for
(PRD Flow D, and the data-leakage mitigation in R5).

---

## 3. Key interaction states

Enumerated because unhandled states are where chat UIs actually fail.

### 3.1 Conversation states

| State | Treatment |
|---|---|
| **Empty session** | Not a blank void. A short line on what the assistant is grounded in, plus 3–4 example questions that are *known to be answerable* from the corpus. First-run success is the whole ballgame. |
| **Composing** | Auto-growing textarea to ~8 rows, then internal scroll. Send disabled while empty or in-flight. |
| **Routing** | Inline status: "Choosing approach…" |
| **Retrieving** | "Searching transcripts…" → resolves to source chips with episode names |
| **Streaming** | Text appends with a caret. **Stop** replaces Send. |
| **Complete** | Actions appear: Copy, Write essay, Make artifact, Regenerate |
| **Abstained** | Distinct visual treatment (muted, bordered — *not* red). Explains the corpus does not cover it, and offers in-corpus topics. Not an error. |
| **Error** | Inline card: what failed, the hint from the error envelope, and Retry. Session stays usable. |
| **Stopped** | Partial output preserved and marked "stopped", not discarded. |

The **abstained ≠ error** distinction is the one I would defend hardest. Styling abstention
red teaches users the product is unreliable; styling it as a calm, informative state teaches
them it is honest. Same behavior, opposite trust outcome.

### 3.2 Artifact viewer states

| State | Treatment |
|---|---|
| **Generating** | Pane opens immediately on `artifact_open` with a titled skeleton, so the user sees *where* output will land before it lands |
| **Preview** | Sandboxed iframe (HTML) or rendered Markdown |
| **Source** | Read-only code view with copy |
| **Sanitized** | Amber inline notice: "2 elements removed" → expands to the report |
| **Render failure** | Falls back to Source view with an explanation. Never a blank frame. |
| **Empty** | Pane not rendered at all |

### 3.3 Session list states

Loading (skeleton rows) · empty (single "New chat" affordance) · active (left border +
raised contrast) · renaming (inline field, Enter commits, Esc cancels) · deleting
(confirm inline, no modal).

---

## 4. Responsive behavior

Three breakpoints, chosen from content needs rather than device names.

| Range | Layout |
|---|---|
| **≥ 1280px** | All three columns. Artifact pane 45%, user-resizable via drag handle. |
| **900–1279px** | Sidebar collapses to icons or overlays on demand. Chat + artifact split 50/50. |
| **< 900px** | Single column. Chat and artifact become **tabs**, with an artifact tab badge when one exists. Sidebar becomes an overlay drawer with a scrim. |
| **< 400px** | Composer actions collapse into an overflow menu; source chips scroll horizontally. |

Two commitments: the artifact viewer is never merely hidden on small screens — it becomes a
tab, because on mobile the artifact is often the *only* thing the user wants. And the layout
is verified at **360 px** (PRD AC13), not at a comfortable 768.

---

## 5. Visual system

Deliberately restrained. This is an internal tool that people read dense text in for long
stretches; the design job is legibility and hierarchy, not personality.

- **Type:** system font stack (no webfont round-trip, no layout shift). Body 15px/1.65.
  Answer measure capped at ~72ch — the single highest-impact readability decision for a
  product whose output is paragraphs.
- **Monospace** for source view and code.
- **Color:** neutral greys carry the UI; one accent for interactive elements. Semantic
  colors are reserved: amber = sanitization notice, red = error only. Abstention uses
  neutral, per §3.1.
- **Theme:** light and dark, both defined via CSS custom properties on `:root`, following
  `prefers-color-scheme` with a manual override persisted locally.
- **Density:** generous line-height in prose, tight in the session list. Different content,
  different rhythm.
- **Motion:** ~150ms ease-out on state transitions; respects `prefers-reduced-motion`.
  The streaming caret is the only continuous animation.

---

## 6. Accessibility

Treated as correctness, not polish.

- **Semantics:** the transcript is a landmark region; messages are list items with
  `role="article"`; the composer is a real `<form>` with a labeled `<textarea>`.
- **Live regions:** the streaming answer is `aria-live="polite"` with `aria-busy` during
  generation, so a screen-reader user gets the answer without the caret animation spamming
  updates. Status transitions (routing/retrieving) announce once, not per token.
- **Keyboard:** every action reachable by Tab in DOM order. Enter sends, Shift+Enter
  newlines, `Esc` closes the artifact pane and cancels inline rename, `Ctrl/Cmd+K` focuses
  the composer. Visible focus rings — never `outline: none` without a replacement.
- **The iframe is labeled** with `title="Rendered artifact preview"`. An unlabeled iframe is
  a dead end for screen readers, and this one contains the product's main output.
- **Contrast:** ≥ 4.5:1 for body text, ≥ 3:1 for large text and UI boundaries, verified in
  both themes.
- **Targets:** ≥ 44×44px on touch.
- **Not conveyed by color alone:** the sanitization notice carries an icon and text;
  abstention is announced in prose, not implied by styling.

---

## 7. Design decisions and trade-offs

**Sources before prose, not after.** Costs a beat of visual noise mid-stream. Buys the P1
promise: grounding is visible *while* the answer forms, which is when the user is deciding
whether to trust it. Post-hoc citations get read as decoration.

**Artifact in a side pane, not a modal.** A modal would be simpler and would dodge the
responsive work in §4. Rejected because artifact generation is iterative — the user reads
the artifact and asks for a change. A modal makes that a repeated open/close cycle; a side
pane makes it a conversation.

**No markdown editor for artifacts.** Considered, cut. It implies durable authorship, which
implies versioning, conflict handling, and autosave — a second product (PRD §1.4).
Regenerate-by-asking keeps the model as the single author and the chat as the single interface.

**Sanitization is visible, not silent.** An amber notice on an otherwise clean artifact is a
small aesthetic cost. Silent stripping is worse: the user sees a subtly broken artifact,
cannot tell why, and files a bug. Naming the removal converts a mystery into a rule the user
learns once.

**Provider badge always on screen.** Uses permanent header space for something most users
change rarely. Justified because the answer's provenance changes its meaning — and because
a user must never be unsure whether their conversation just went to a cloud API (PRD R5).

**System fonts over a brand face.** Gives up visual distinctiveness. Buys zero webfont
latency, zero layout shift, and native rendering on every platform. For an internal tool
read all day, that is the right trade.

**Stop preserves partial output.** Discarding on stop is easier to implement. But a user who
stops a 30-second local generation usually stops because they already got what they needed —
throwing it away punishes the exact behavior slow local inference encourages.
