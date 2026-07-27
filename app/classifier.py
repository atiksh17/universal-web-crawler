"""Deterministic success/failure gate. No LLM.

Decides, per tier result: SUCCESS (return to client) or FAIL (escalate). The hard
case is the *soft failure* — HTTP 200 whose body is a block page, a challenge
interstitial, or an unrendered SPA shell. Returning those as success is exactly
what made the old browserless system "miss content". This gate stops that.

Two-phase, NEGATIVE-FIRST (per design):
  PHASE 1 — negative signals. If ANY known provider block/challenge is detected,
            fail immediately and skip the positive phase. (Cheap, high precision.)
  PHASE 2 — positive signals. Only reached when phase 1 is clean. Require at least
            one structural proof a REAL page loaded (substantive text + links /
            landmarks / structured data). No positive proof => fail (blocked/thin).

Rationale: a clean negative scan makes real content overwhelmingly likely, and a
real page almost always trips multiple positive signals at once. A challenge
interstitial trips none.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .models import FetchResult
from .signatures import find_block_signature, find_challenge_host, find_interstitial_title

try:
    from selectolax.parser import HTMLParser
    _HAS_SELECTOLAX = True
except Exception:  # pragma: no cover
    _HAS_SELECTOLAX = False

ACCEPTABLE_STATUS = {200, 203, 206}
BROWSER_TIERS = {"L2", "L2B", "L2C", "L3"}
# Above this much visible text a vendor-name / challenge-host match is incidental
# (an article about bot detection, or a page embedding a captcha widget in a form) —
# NOT a block. Keeps the host/string negatives high-precision. Interstitial TITLES
# are exempt from this gate (they are unambiguous).
BLOCK_MAX_TEXT = 2000


def _tree(html: str):
    return HTMLParser(html) if _HAS_SELECTOLAX else None


def visible_text_len(html: str, tree=None) -> int:
    """Length of human-visible text (scripts/styles stripped). Drives thin detection."""
    if not html:
        return 0
    if _HAS_SELECTOLAX:
        tree = tree or HTMLParser(html)
        for tag in tree.css("script, style, noscript, template"):
            tag.decompose()
        node = tree.body or tree.root
        return len(node.text(separator=" ", strip=True) if node else "")
    import re
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    return len(re.sub(r"<[^>]+>", " ", text).strip())


def _title_lower(html: str, tree) -> str:
    if tree is not None:
        node = tree.css_first("title")
        if node:
            return (node.text() or "").strip().lower()
        return ""
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    return (m.group(1).strip().lower() if m else "")


def positive_signals(tree, base_url: str) -> dict:
    """Structural proof a REAL page loaded. Each is something an interstitial lacks.

    Returns a dict of booleans/counts; the gate decides how many must fire.
    """
    if tree is None:
        return {"anchors": 0, "landmark": False, "structured": False, "images": 0}
    host = (urlparse(base_url).hostname or "").lower()

    anchors = 0
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip().lower()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        # same-origin (relative, or absolute to the same host) — real site navigation
        if href.startswith("/") or (host and host in href):
            anchors += 1

    landmark = tree.css_first("main, article, footer, nav, header, [role='main']") is not None
    structured = tree.css_first(
        "script[type='application/ld+json'], meta[property^='og:'], link[rel='canonical']"
    ) is not None
    images = len(tree.css("img"))
    return {"anchors": anchors, "landmark": landmark, "structured": structured, "images": images}


def classify(result: FetchResult, *, min_text_len: int, min_render_text_len: int,
             min_anchors: int = 8) -> tuple[bool, str]:
    """Return (ok, reason). ok=False => escalate; reason explains why and steers routing."""
    # 0. HTTP status
    if result.status is not None and result.status not in ACCEPTABLE_STATUS:
        return False, f"http_{result.status}"

    html = result.html or ""
    if not html.strip():
        return False, "empty_response"

    tree = _tree(html)
    tlen = result.text_len or visible_text_len(html, tree)
    result.text_len = tlen
    html_lower = html.lower()

    # Positive signals computed up front — needed both to guard the body/host negatives
    # (a real page can embed a captcha widget) and for the positive phase below.
    pos = positive_signals(tree, result.final_url or result.url)
    min_t = min_render_text_len if result.tier in BROWSER_TIERS else min_text_len
    structural = (pos["anchors"] >= min_anchors) or pos["landmark"] or pos["structured"]
    # ALL three structural signals at once = provably a real page even with thin body
    # text (design-heavy sites render copy as images/canvas). No interstitial clears this
    # bar — challenge pages have 0 same-origin links, no landmark, no structured data.
    strong_structure = (pos["anchors"] >= min_anchors) and pos["landmark"] and pos["structured"]
    looks_real = (tlen >= min_t and structural) or strong_structure

    # ============================== PHASE 1: NEGATIVE ==============================
    # 1a. Interstitial <title> — UNGATED, unambiguous. A real page's title is its own
    # name; only a challenge/block page titles itself "Just a moment" / "Security
    # Checkpoint" / etc. Always a block, even if other signals look real.
    title = _title_lower(html, tree)
    hit = find_interstitial_title(title)
    if hit:
        return False, f"block:{hit}"

    # 1b/1c. Challenge host + body signatures — AMBIGUOUS (a real page can embed a
    # reCAPTCHA/Turnstile widget in a form, or name a vendor). Block only when the page
    # is thin AND does NOT independently prove it's real. `looks_real` override stops the
    # false block on e.g. a real "GGV Capital" page that embeds a captcha widget.
    if tlen < BLOCK_MAX_TEXT and not looks_real:
        hit = find_challenge_host(html_lower) or find_block_signature(html_lower)
        if hit:
            return False, f"block:{hit}"

    # ============================== TIER PRE-GATE ==================================
    has_script = "<script" in html_lower
    # L1 cannot run JS: a thin page WITH scripts is a likely unrendered SPA shell ->
    # escalate to a browser. (A thin page with NO scripts is a small complete page;
    # known blocks were already caught above, so let the positive phase judge it.)
    if result.tier == "L1" and tlen < min_text_len and has_script:
        return False, "empty_shell_needs_js"

    # ============================== PHASE 2: POSITIVE ==============================
    # Real page: substantive text + a structural signal, OR strong structure alone.
    if looks_real:
        return True, ""
    # Small but genuine static page (L1, no JS to gain from a browser hop): accept if
    # it has either real text or any structure — interstitials have neither.
    if result.tier == "L1" and not has_script and (tlen >= min_text_len or structural or pos["images"]):
        return True, ""

    reason = "thin_content_after_render" if result.tier in BROWSER_TIERS else "no_positive_signal"
    return False, reason
