# Planned Features

Backlog of features to build later. Nothing here is implemented yet.

---

## 1. Client feedback loop — production-driven accuracy improvement

**Status:** Proposed (do not build yet — documented so it isn't forgotten).

### Goal
Let the scraper get **progressively better over time using real examples from real
production clients**, instead of only the block/challenge signatures and classifier rules
we hand-author up front.

### The problem it solves
The classifier is deterministic (negative signals → positive confirmation) and high
precision today. But it can only catch what it already knows about:

- **Known providers/patterns** (Cloudflare, Vercel, DataDome, reCAPTCHA, …) are caught.
- **Novel cases** are not. A brand-new provider, a redesigned checkpoint, or an unusual
  page shape can slip through as a **false positive** (a block/empty page wrongly counted
  as a successful scrape) or a **false negative** (real content wrongly flagged as a
  failure). This risk is highest at **L3** (Camoufox) and any future tier, where outputs
  are more varied.

The system cannot self-identify these novel mislabels — by definition they aren't in its
rules yet. **But the client can.**

### Key insight: the client is an AI agent
Every client that calls this system does so through its **own Claude Code (AI agent)
instance**. After the agent receives the scraped data, it actually reads and verifies it
for its own task. So the agent is in a position to notice — and correctly **label** — when
our system got it wrong:

- We returned `ok=true` but the agent sees it's actually a block/empty page → **false positive**.
- We returned a failure but the agent (via other means) knows real content existed, or we
  handed back partial/garbled content → **false negative**.

The agent understands the content, so it can label these for us — something a dumb client
could not do.

### Proposed solution: a second feedback endpoint
Add a **second API endpoint on the same base URL** that lets a client submit corrections
after the fact. Roughly:

```
POST /feedback
{
  "items": [
    {
      "url": "https://www.example.com",
      "job_id": "…",                 // optional: links back to the original scrape
      "our_verdict": { "ok": true, "tier": "L3", "reason": "" },
      "client_label": "false_positive",   // false_positive | false_negative
      "evidence": "page is a Vercel checkpoint, title 'Security Checkpoint', no real content",
      "captured_html_excerpt": "…"   // optional: lets us mine new signatures
    }
  ]
}
```

The endpoint just **logs** these labeled corrections — it does not change behavior at
request time. It builds a growing corpus of **fresh, real-world examples where the system
was wrong**, sourced from actual production traffic and pre-labeled by the calling agent.

### How it improves the system over time
The accumulated feedback corpus becomes the input to periodic improvement:

- **New block signatures** — false positives reveal challenge/empty pages we don't yet
  fingerprint → add to `app/signatures.py`.
- **Classifier tuning** — false negatives reveal real pages we wrongly reject → adjust
  positive-signal thresholds / structural rules in `app/classifier.py`.
- **Regression set** — every logged example becomes a test case, so a future change that
  re-breaks it is caught.

Net effect: the scraper's accuracy compounds from real production data instead of staying
fixed at whatever we hand-coded, while preserving the **zero-false-data** goal — every
correction makes the deterministic gate a little more truthful.

### Open questions (decide at build time)
- **Storage**: where the corpus lives (same SQLite store, a dedicated table, or append-only
  JSONL) and its retention.
- **Auth / trust**: how to authenticate clients and weight/validate submitted labels (a
  client could mislabel); possibly require the `captured_html_excerpt` as evidence.
- **Apply loop**: manual review → signature/threshold update, vs. a semi-automated proposal
  step that suggests new signatures from clustered false positives.
- **Schema**: final request/response shape, idempotency (dedupe repeat reports of the same
  url+verdict), and linkage back to the original `job_id`/attempt.
