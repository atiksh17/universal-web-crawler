"""Deterministic success/failure gate. No LLM.

Decides, per tier result: SUCCESS (return to client) or FAIL (escalate). The hard
case is the *soft failure* — HTTP 200 whose body is a block page, a challenge
interstitial, or an unrendered SPA shell. Returning those as success is exactly
what made the old browserless system "miss content". This gate stops that.
"""
from __future__ import annotations

from .models import FetchResult
from .signatures import find_block_signature

try:
    from selectolax.parser import HTMLParser
    _HAS_SELECTOLAX = True
except Exception:  # pragma: no cover
    _HAS_SELECTOLAX = False

ACCEPTABLE_STATUS = {200, 203, 206}
BROWSER_TIERS = {"L2", "L2B", "L2C", "L3"}
# Real challenge/block pages are small ("Just a moment...", "Access Denied"). Above this
# much visible text, a vendor-name match is incidental (an article about bot detection, or
# a page embedding a captcha widget) — NOT a block. Prevents false escalation on real content.
BLOCK_MAX_TEXT = 2000


def visible_text_len(html: str) -> int:
    """Length of human-visible text (scripts/styles stripped). Drives shell detection."""
    if not html:
        return 0
    if _HAS_SELECTOLAX:
        tree = HTMLParser(html)
        for tag in tree.css("script, style, noscript, template"):
            tag.decompose()
        node = tree.body or tree.root
        text = node.text(separator=" ", strip=True) if node else ""
        return len(text or "")
    import re
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return len(text.strip())


def classify(result: FetchResult, *, min_text_len: int, min_render_text_len: int) -> tuple[bool, str]:
    """Return (ok, reason). ok=False => escalate; reason explains why and steers routing."""
    # 1. HTTP status
    if result.status is not None and result.status not in ACCEPTABLE_STATUS:
        return False, f"http_{result.status}"

    html = result.html or ""
    if not html.strip():
        return False, "empty_response"

    tlen = result.text_len or visible_text_len(html)
    result.text_len = tlen
    has_script = "<script" in html.lower()

    # 2. Block-signature scan — gated on short content (real blocks are small interstitials;
    # a long page that merely mentions/embeds a vendor is not blocked).
    if tlen < BLOCK_MAX_TEXT:
        sig = find_block_signature(html.lower())
        if sig:
            return False, f"block:{sig}"

    # 3. Empty-shell detection — L1 cannot run JS.
    # Only escalate "thin" when scripts are present (possible unrendered SPA). A short
    # page with no JS is a small *complete* page — escalating it to a browser adds
    # nothing (no JS to run) and only burns a hop, then paid tiers. Real blocks were
    # already caught by the signature scan above; truly empty by empty_response.
    if result.tier == "L1":
        if tlen < min_text_len and has_script:
            return False, "empty_shell_needs_js"
        return True, ""

    # 4. Render-completeness — browser tiers
    if result.tier in BROWSER_TIERS:
        if tlen < min_render_text_len:
            return False, "thin_content_after_render"
        return True, ""

    # L4 / catch-all: trust if there's content and no block signature
    if tlen < min_render_text_len:
        return False, "thin_content"
    return True, ""
