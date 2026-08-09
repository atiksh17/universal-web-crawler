"""Response shaping — turn raw scraped HTML into the payload the caller asked for.

Mirrors the old n8n "Shape Response" node. ALWAYS returns: domain, markdown, quality,
bot_blocked (+ error when failed). OPTIONAL, included only when the caller sets the flag
true in the request: endpoints, metas, footerHtml, html (raw, or selector-scoped).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

try:
    from selectolax.parser import HTMLParser
    _HAS = True
except Exception:  # pragma: no cover
    _HAS = False

_DROP = {"script", "style", "noscript", "template", "svg"}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


class ShapeOptions:
    """Which optional fields the caller wants. All default OFF (markdown always on).

    `md_links` shapes the ALWAYS-on markdown — three modes, normalized here to a mode string:

      True  / "inline" — `[text](href)`, the full markdown link
      False / "text"   — anchor TEXT only, the `(href)` dropped
      "strip"          — the anchor contributes NOTHING: no href, no label

    Default None = auto: `text` when `endpoints` is on (that list already carries every same-origin
    URL, so inline hrefs are a second copy), `inline` when it's off. `strip` is never automatic —
    it deletes words a sentence may need, so the caller has to ask for it.
    """

    def __init__(self, endpoints=False, meta=False, footerHtml=False, html=False, selector=None,
                 md_links=None):
        self.endpoints = bool(endpoints)
        self.meta = bool(meta)
        self.footerHtml = bool(footerHtml)
        self.html = bool(html)
        self.selector = selector or None
        self.md_links = md_link_mode(md_links, self.endpoints)


# markdown link modes
LINKS_INLINE, LINKS_TEXT, LINKS_STRIP = "inline", "text", "strip"


def md_link_mode(md_links, endpoints: bool) -> str:
    """Normalize the caller's `md_links` (None | bool | mode string) to a mode constant."""
    if md_links is None:                                  # auto
        return LINKS_TEXT if endpoints else LINKS_INLINE
    if isinstance(md_links, str):
        m = md_links.strip().lower()
        return m if m in (LINKS_INLINE, LINKS_TEXT, LINKS_STRIP) else LINKS_INLINE
    return LINKS_INLINE if md_links else LINKS_TEXT


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# ----------------------------- markdown -----------------------------
def _inline(node, links: str = LINKS_INLINE) -> str:
    out = []
    for c in node.iter(include_text=True):
        tag = c.tag
        if tag == "-text":
            out.append(c.text(deep=False) or "")
        elif tag in _DROP:
            continue
        elif tag == "a":
            if links == LINKS_STRIP:      # anchor contributes nothing — not even its label
                continue
            href = (c.attributes.get("href") or "").strip()
            txt = _inline(c, links).strip()
            out.append(f"[{txt}]({href})" if links == LINKS_INLINE and href and txt else txt)
        elif tag in ("strong", "b"):
            out.append(f"**{_inline(c, links).strip()}**")
        elif tag in ("em", "i"):
            out.append(f"*{_inline(c, links).strip()}*")
        elif tag == "br":
            out.append("\n")
        else:
            out.append(_inline(c, links))
    return "".join(out)


def _block(node, parts: list, links: str = LINKS_INLINE) -> None:
    tag = node.tag
    if tag in _DROP:
        return
    if tag in _HEADINGS:
        parts.append(f"\n{_HEADINGS[tag]} {_inline(node, links).strip()}\n")
        return
    if tag in ("p", "blockquote"):
        txt = _inline(node, links).strip()
        if txt:
            parts.append(("> " + txt if tag == "blockquote" else txt) + "\n")
        return
    if tag in ("ul", "ol"):
        for li in node.css("li"):
            t = _inline(li, links).strip()
            if t:
                parts.append(f"- {t}")
        parts.append("")
        return
    if tag in ("li", "a", "strong", "b", "em", "i", "span", "-text"):
        return  # handled inline by ancestors
    # generic container: recurse into children
    for c in node.iter(include_text=False):
        _block(c, parts, links)


def to_markdown(html: str, links: str = LINKS_INLINE) -> str:
    """Page text as markdown. `links` is a mode (see ShapeOptions): `inline` keeps `[text](href)`,
    `text` keeps the label alone, `strip` drops anchors entirely. Bools are accepted as the old
    True/False spelling of inline/text.

    Under `strip`, a block that was ONLY links (nav bars, link lists) collapses to nothing and is
    dropped — that's the point — while prose keeps its non-anchor words."""
    if isinstance(links, bool) or links is None:
        links = md_link_mode(links, endpoints=False)
    if not html or not _HAS:
        return ""
    tree = HTMLParser(html)
    body = tree.body or tree.root
    if body is None:
        return ""
    parts: list = []
    for c in body.iter(include_text=False):
        _block(c, parts, links)
    md = "\n".join(parts)
    if links == LINKS_STRIP:
        # A removed anchor leaves a hole mid-sentence: "read the  ." Close the gap so the prose
        # that survives reads cleanly (run-together spaces, then space-before-punctuation).
        md = re.sub(r"[ \t]{2,}", " ", md)
        md = re.sub(r" +([,.;:!?)\]])", r"\1", md)
        md = re.sub(r"([(\[]) +", r"\1", md)
    # collapse 3+ newlines to 2, trim
    while "\n\n\n" in md:
        md = md.replace("\n\n\n", "\n\n")
    return md.strip()


# ----------------------------- optional extractors -----------------------------
def extract_metas(tree) -> list:
    metas = []
    for m in tree.css("meta"):
        a = m.attributes
        entry = {k: a.get(k) for k in ("name", "property", "content", "charset", "http-equiv") if a.get(k)}
        if entry:
            metas.append(entry)
    return metas


def extract_footer(tree) -> str | None:
    node = tree.css_first("footer")
    return node.html if node else None


def extract_endpoints(tree, base_url: str) -> list:
    """On-site link tree: unique same-origin links as {url, text}."""
    host = domain_of(base_url)
    seen, out = set(), []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absu = urljoin(base_url, href)
        if domain_of(absu) != host:
            continue
        absu = absu.split("#", 1)[0]
        if absu in seen:
            continue
        seen.add(absu)
        out.append({"url": absu, "text": (a.text() or "").strip()[:120]})
    return out


def _scoped_html(html: str, tree, selector: str | None) -> str:
    if not selector:
        return html
    return "\n".join(n.html or "" for n in tree.css(selector))


def _quality(ok: bool, reason: str) -> tuple[str, bool]:
    """ok | needs_retry | bot_blocked  (+ bot_blocked flag)."""
    if ok:
        return "ok", False
    if reason.startswith("block:"):
        return "bot_blocked", True
    return "needs_retry", False


def shape_response(url: str, html: str, ok: bool, reason: str, opts: ShapeOptions) -> dict:
    quality, bot_blocked = _quality(ok, reason)
    out = {
        "domain": domain_of(url),
        "markdown": to_markdown(html, opts.md_links),
        "quality": quality,
        "bot_blocked": bot_blocked,
    }
    if not ok and reason:
        out["error"] = reason

    needs_tree = opts.endpoints or opts.meta or opts.footerHtml or (opts.html and opts.selector)
    tree = HTMLParser(html) if (html and _HAS and needs_tree) else None

    if opts.endpoints:
        out["endpoints"] = extract_endpoints(tree, url) if tree else []
    if opts.meta:
        out["metas"] = extract_metas(tree) if tree else []
    if opts.footerHtml:
        out["footerHtml"] = extract_footer(tree) if tree else None
    if opts.html:
        out["html"] = _scoped_html(html, tree, opts.selector) if tree else (html or "")
    return out
