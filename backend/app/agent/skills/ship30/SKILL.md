# Skill: Ship 30 for 30 Essay

**Version:** 1.0
**Purpose:** Turn a grounded, transcript-backed answer into a publishable
long-form essay in the Ship 30 for 30 style.

> **This file is the skill.** It is loaded from disk at runtime and is not baked
> into Python. A content lead can revise house style by editing this file and
> restarting — no engineer, no deploy. Keep it operational: every principle
> below should be checkable by a person reading a draft.

---

## 1. The writing methodology

Ship 30 for 30 is a public writing methodology built around the **atomic
essay** — one self-contained idea, written to be read start to finish in a
single sitting, formatted so it can be skimmed in fifteen seconds and still
land. The principles below are the operational encoding of that approach.

### P1 — One idea, and only one

The essay makes a single argument. Supporting points exist to advance that
argument, never to introduce a second one. If a section could be its own
essay, cut it and note it as a follow-up.

**Check:** you can state the essay's claim in one sentence without using "and".

### P2 — The hook's only job is to earn the second line

The first line is not a summary, a definition, or a throat-clear. It creates a
**curiosity gap** — a reason to keep reading that the reader cannot resolve
without continuing.

Effective hook shapes:

- **Counterintuitive claim** — states something that contradicts the obvious.
- **Specific stake** — a concrete number, cost, or consequence.
- **Named tension** — two things the reader believes that cannot both be true.
- **Sharp question** — one the reader cannot answer immediately.

**Never open with:** "In today's fast-paced world", "Product-market fit is
one of the most important…", "Have you ever wondered…", or any dictionary
definition.

**Check:** delete the first line. If the essay reads the same, the hook was
decoration, not a hook.

### P3 — Narrative progression, not a list of thoughts

Sections build. Each one earns the next. The reader should never be able to
reorder two sections without the essay getting worse.

The default progression:

1. **Hook** — open the loop.
2. **Stakes** — why this matters now, and what it costs to get wrong.
3. **The shift** — the insight that reframes the problem.
4. **The mechanism** — how it actually works, with evidence.
5. **The application** — what the reader does differently.
6. **Takeaway** — close the loop opened in the hook.

**Check:** each section's first sentence references or advances the one before.

### P4 — Skimmable by construction

A reader scanning headings and bold text alone should get the argument.

- **Headings** are claims, not labels. "Retention is a leading indicator" beats
  "Retention".
- **Paragraphs** run 1–3 sentences. A four-line block of prose is a rewrite signal.
- **Bold** marks the sentence you would want a skimmer to read — roughly one per
  section. Bolding many phrases is the same as bolding none.
- **Bullets** carry parallel items only. If items are not parallel, they are prose.

**Check:** read only the headings and bold text. Is the argument intact?

### P5 — Specific beats abstract, always

Name the company, the number, the person, the situation. "Several companies
improved onboarding" is filler; a named operator describing a specific change
is an argument.

Because this assistant is transcript-grounded, specificity and citation are the
same act: the specific detail comes from a source, and the source is cited.

**Check:** every claim either carries a citation or is explicitly framed as the
author's synthesis.

### P6 — Write to one person

Second person, present tense, conversational register. Contractions are fine.
The reader is a smart practitioner who is busy, not a student.

**Ban list:** "leverage" (as a verb), "utilize", "delve", "myriad",
"in the realm of", "it is important to note that", "furthermore", "moreover",
"in conclusion", "landscape" (figurative), "unlock" (figurative), "game-changer".

### P7 — The takeaway is a verb

The essay ends with something the reader can do this week — a specific action,
not an inspirational restatement. It must close the loop the hook opened.

**Check:** the takeaway names an action, a first step, and how the reader knows
it worked.

---

## 2. Structural contract

Enforced automatically by `validators.py`. A draft that fails is repaired
section-by-section rather than regenerated whole.

| Requirement | Target | Hard bounds |
|---|---|---|
| Total length | ~1,250 words | 1,100–1,400 |
| H2 sections (`##`) | 4–6 | ≥3 |
| Bullet lists | ≥1 | ≥1 |
| Bold spans | 4–8 | ≥3, ≤20 |
| Citations `[S#]` | ≥3 | ≥2 |
| Paragraph length | 1–3 sentences | ≤4 |
| Title | One `#` H1 | exactly 1 |
| Takeaway section | Required | must be the final section |

---

## 3. Grounding rules

These override every stylistic principle above. Style serves the argument; the
argument serves the evidence.

1. **Every factual claim traces to a source.** Use `[S1]`, `[S2]` inline,
   matching the numbered sources supplied in context.
2. **Never invent a statistic, quote, company, or outcome.** If the evidence
   does not support a point, cut the point — do not soften it into a vaguer
   version that sounds supported.
3. **Attribute by name** where the source names a person: "As <guest> describes
   it…" is both better writing and better grounding.
4. **Synthesis is allowed and must be marked.** Connecting two sources into a
   new observation is the essay's value. Frame it as the author's reasoning,
   not as something a source said.
5. **Do not pad to hit the word count.** A tight 1,150-word essay beats a
   padded 1,250-word one. The count is a target, not a quota.

---

## 4. Anti-patterns

Reject a draft exhibiting any of these:

- **The listicle in disguise** — sections that could be reordered freely (violates P3).
- **The definition opener** — beginning by defining the topic (violates P2).
- **Bold soup** — more than 20 bold spans; emphasis stops meaning anything (P4).
- **The uncited assertion** — a confident claim with no `[S#]` (grounding rule 1).
- **The inspirational ending** — "the future belongs to teams that…" (violates P7).
- **The summary paragraph** — restating what the reader just read instead of
  giving them something to do.
- **Fake specificity** — invented numbers to satisfy P5. Worse than abstraction,
  because it is unfalsifiable-looking and wrong.

---

## 5. Generation procedure

Drafting is **section-wise, not single-shot**. Asking a small local model for
1,250 coherent words in one pass produces drift and repetition; each call here
stays inside the model's reliable working range (PRD R3).

1. **Plan** — from the grounded answer and retrieved sources, produce a title,
   a one-sentence thesis, and 4–6 section headings that form a progression (P3).
2. **Hook** — draft the opening (~120 words) against P2.
3. **Sections** — draft each section (~200–250 words) with only the evidence
   relevant to it in context.
4. **Takeaway** — draft the close (~120 words) against P7, explicitly closing
   the hook's loop.
5. **Assemble** — concatenate, then renumber citations so `[S#]` markers are
   contiguous and every marker resolves.
6. **Validate** — run the structural contract. On failure, repair only the
   offending section.
