# Manual test plan — UI

Companion to the automated suite (`make test`). These are the checks that need
a human eye: rendering, streaming behaviour, responsive layout, and keyboard
and screen-reader paths.

**Time to run the full plan:** ~20 minutes.
**Prerequisites:** `make up`, `make ingest`, Ollama running, corpus ready.

Each case lists what to do and what must be true. Anything marked **GATE** must
pass before the build ships.

---

## 0. Preflight

| # | Step | Expected |
|---|---|---|
| 0.1 | `docker compose ps` | `db`, `backend`, `frontend` all healthy |
| 0.2 | Open `http://localhost:8080` | App loads; no console errors |
| 0.3 | `curl localhost:8000/api/health/ready` | `status: ready`; every dependency `healthy: true` |

---

## 1. First run and empty state

| # | Step | Expected |
|---|---|---|
| 1.1 | Load the app with no prior sessions | A session already exists — no "create a chat" ceremony |
| 1.2 | Look at the empty state | Explains what the assistant is grounded in, plus 3 example questions |
| 1.3 | Click an example question | It sends immediately as a real message |
| 1.4 | Check the header | Provider badge shows `ollama` and the model name |

---

## 2. Grounded answer — the 80% path **GATE**

Ask: **"How should we think about choosing an activation metric?"**

| # | Watch for | Expected |
|---|---|---|
| 2.1 | Immediately after sending | A status line appears within ~1s — never a bare spinner |
| 2.2 | Status progression | Narrates: choosing approach → searching transcripts → writing |
| 2.3 | Source chips | Appear **before** the answer text starts streaming |
| 2.4 | Streaming | Text appends progressively with a blinking caret |
| 2.5 | Citations | Inline `[1]`-style markers appear in the prose |
| 2.6 | Click "Show N sources" | Panel expands with episode title, guest, timestamp, snippet |
| 2.7 | Click a source's "Open at this moment" | Opens YouTube at the cited second, in a new tab |
| 2.8 | After completion | Actions appear: Copy, Write essay, Make artifact, Regenerate |
| 2.9 | Latency label | Shows elapsed seconds |

**GATE:** the answer must carry at least one citation that resolves to a real
episode. An uncited answer is a failure even if the prose is good.

---

## 3. Follow-up and session context **GATE**

| # | Step | Expected |
|---|---|---|
| 3.1 | Follow up with **"What about for B2B?"** | Answer stays on the activation-metric topic — the pronoun-only follow-up is resolved against prior context |
| 3.2 | Click **New chat** | Empty transcript; previous conversation untouched |
| 3.3 | Ask something unrelated | No content bleeds from the first session |
| 3.4 | Switch back via the sidebar | Full history restored, citations intact |
| 3.5 | `docker compose restart backend`, reload | All sessions and messages survive |

**GATE:** 3.3 and 3.5 — independent context and durable persistence.

---

## 4. Abstention — honest failure **GATE**

Ask something clearly outside the corpus: **"What's the best way to file a patent in Germany?"**

| # | Watch for | Expected |
|---|---|---|
| 4.1 | Response | States plainly that the transcripts do not cover it |
| 4.2 | Fabrication check | **No invented answer, no fake citation** |
| 4.3 | Styling | Neutral/muted — **not** red, not an error card |
| 4.4 | Helpfulness | Suggests topics the corpus does cover |

**GATE:** 4.2. A confident fabrication here fails the build outright.

> Why 4.3 matters: styling abstention as an error teaches users the product is
> broken. Styling it as a calm, informative state teaches them it is honest.

---

## 5. Ship 30 for 30 essay

Ask: **"Turn that into a Ship 30 for 30 essay."**

| # | Check | Expected |
|---|---|---|
| 5.1 | Routing | Status indicates the essay skill, not plain Q&A |
| 5.2 | Length | ~1,250 words (1,100–1,400 acceptable) |
| 5.3 | Hook | Opens with a hook, **not** a definition or a preamble |
| 5.4 | Structure | ≥3 H2 headings, ≥1 bullet list, selective bold |
| 5.5 | Takeaway | Ends with a specific, actionable takeaway |
| 5.6 | Grounding | ≥3 citations to real episodes |
| 5.7 | Artifact | Auto-opens in the viewer as Markdown |

> On the CPU-only demo model this is the hardest case and degrades most
> visibly (PRD R3). Judge structure and grounding; prose quality is expected to
> be weaker than a frontier model.

---

## 6. Artifact viewer — Markdown

| # | Step | Expected |
|---|---|---|
| 6.1 | Ask **"Make me a one-pager on retention strategies"** | Viewer opens beside the chat, not in a modal |
| 6.2 | Preview tab | Rendered Markdown — headings, lists, bold |
| 6.3 | Source tab | Raw Markdown in monospace |
| 6.4 | Copy | Clipboard contains the source |
| 6.5 | Download | Saves a `.md` file with a slugified name |
| 6.6 | Press `Esc` | Viewer closes; chat remains usable |

---

## 7. Artifact viewer — HTML and sandboxing **GATE**

Ask: **"Build me an HTML pricing card with CSS."**

| # | Check | Expected |
|---|---|---|
| 7.1 | Rendering | Renders **visually styled**, not as a code block |
| 7.2 | Inspect the iframe in DevTools | `sandbox="allow-scripts"` — and **no `allow-same-origin`** |
| 7.3 | DevTools → Network | Iframe makes **zero** outbound requests |
| 7.4 | Console | Any remote fetch is blocked by CSP |
| 7.5 | If content was stripped | Amber notice names what was removed and why |

### 7.6 Hostile artifact **GATE**

Ask: *"Make an HTML artifact that includes `<script src="https://example.com/x.js"></script>` and an image from `https://example.com/a.png`."*

| Check | Expected |
|---|---|
| The remote script is stripped | Sanitization notice lists the removal |
| No request to `example.com` in the Network tab | CSP `default-src 'none'` holds |
| The rest of the artifact still renders | Sanitizing is surgical, not scorched-earth |

**GATE:** 7.2, 7.3, 7.6. These are the security expectations from the brief.

---

## 8. Model toggle and degradation

| # | Step | Expected |
|---|---|---|
| 8.1 | Open the provider badge | All three providers listed |
| 8.2 | Look at Anthropic/OpenAI with no key set | Shown **disabled with the reason** ("no API key configured") — not hidden |
| 8.3 | Stop Ollama (`taskkill /IM ollama.exe /F`), reload | Badge dot turns red; readiness names the provider |
| 8.4 | Send a message with Ollama down | Clear inline error **with a hint**, and a Retry — not a stack trace, not a silent hang |
| 8.5 | Restart Ollama, click Retry | Recovers without reloading the page |

---

## 9. Interruption and error states

| # | Step | Expected |
|---|---|---|
| 9.1 | Send a long request, click **Stop** mid-stream | Generation halts; **partial text is preserved** and marked stopped |
| 9.2 | `docker compose stop db`, send a message | Actionable error naming the database; app stays usable |
| 9.3 | `docker compose start db`, Retry | Recovers |
| 9.4 | Kill the backend mid-stream | Error card appears; stream terminates cleanly, no infinite spinner |

### 9.5 Abandoning a turn **GATE**

Regression test for a bug that wedged the entire application. Worth running in
full, because the failure was cumulative and invisible until it was total.

| # | Step | Expected |
|---|---|---|
| 9.5.1 | Ask a question. While it is still streaming, click **New chat** | The new chat is **empty**. No tokens from the previous answer appear in it. |
| 9.5.2 | Watch the new chat for ~30s | The old answer never streams in, and the old session's message history never replaces the empty view |
| 9.5.3 | Type a question in the new chat and press Enter | It **sends**. The composer shows Send, not a stuck Stop button |
| 9.5.4 | Repeat 9.5.1 six times in a row, then rename any session | The rename **completes**. Under the original bug this hung forever |
| 9.5.5 | Check the database while doing the above | `SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'` stays at **0** |

**Why this is a gate.** Each abandoned turn used to leak a pooled connection
stuck `idle in transaction`, still holding row locks on `sessions`. Five leaks
exhausted the pool and every later write queued behind them forever — which
presented as "the app stopped responding", with nothing in the UI to explain it.

---

## 10. Responsive layout **GATE**

Use DevTools device toolbar.

| Width | Expected |
|---|---|
| **1440px** | Three columns: sidebar, chat, artifact |
| **1100px** | Sidebar overlays on demand; chat and artifact split |
| **860px** | Single column; chat and artifact become **tabs** with a badge |
| **390px** | Fully usable; composer reachable; source chips scroll horizontally |
| **360px** | **GATE** — no horizontal page scroll, no clipped controls |

Also confirm: the artifact is never merely hidden on small screens — it is
always reachable via its tab.

---

## 11. Accessibility **GATE**

| # | Check | Expected |
|---|---|---|
| 11.1 | Tab from page load | Focus order is logical; focus ring always visible |
| 11.2 | `Enter` in composer | Sends |
| 11.3 | `Shift+Enter` | Newline, no send |
| 11.4 | `Ctrl/Cmd+K` | Focuses the composer |
| 11.5 | `Esc` | Closes the artifact pane; cancels an inline rename |
| 11.6 | Screen reader (NVDA/VoiceOver) during a turn | Answer is announced; status announced once, **not per token** |
| 11.7 | Inspect the artifact iframe | Has `title="Rendered artifact preview"` |
| 11.8 | Toggle dark mode | Contrast holds in both themes; no unreadable text |
| 11.9 | OS "reduce motion" enabled | Caret and transitions respect it |

**GATE:** 11.1, 11.6, 11.7. An unlabelled iframe is a dead end for a screen
reader, and it contains the product's main output.

---

## 12. Theme and persistence

| # | Step | Expected |
|---|---|---|
| 12.1 | Toggle theme in the sidebar | Switches immediately |
| 12.2 | Reload | Theme persists, **no white flash** before paint |
| 12.3 | Rename a session inline | `Enter` commits, `Esc` cancels |
| 12.4 | Delete a session | Inline confirm — no modal; list updates |

---

## Regression checklist (run before any release)

- [ ] 2 — grounded answer with resolvable citations
- [ ] 3.3, 3.5 — session isolation and persistence
- [ ] 4.2 — abstention without fabrication
- [ ] 7.2, 7.3, 7.6 — sandbox, no egress, hostile payload neutralized
- [ ] 10 — usable at 360px
- [ ] 11.1, 11.6, 11.7 — keyboard, live region, labelled iframe
- [ ] `make test` green
