"""Response shaping — turn raw scraped HTML into the payload the caller asked for.

Mirrors the old n8n "Shape Response" node. ALWAYS returns: domain, markdown, quality,
bot_blocked (+ error when failed). OPTIONAL, included only when the caller sets the flag
true in the request: endpoints, metas, footerHtml, html (raw, or selector-scoped).
"""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

try:
    from selectolax.parser import HTMLParser
    _HAS = True
except Exception:  # pragma: no cover
    _HAS = False

_DROP = {"script", "style", "noscript", "template", "svg"}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


class ShapeOptions:
    """Which optional fields the caller wants. All default OFF (markdown always on)."""

    def __init__(self, endpoints=False, meta=False, footerHtml=False, html=False, selector=None):
        self.endpoints = bool(endpoints)
        self.meta = bool(meta)
        self.footerHtml = bool(footerHtml)
        self.html = bool(html)
        self.selector = selector or None


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# ----------------------------- markdown -----------------------------
def _inline(node) -> str:
    out = []
    for c in node.iter(include_text=True):
        tag = c.tag
        if tag == "-text":
            out.append(c.text(deep=False) or "")
        elif tag in _DROP:
            continue
        elif tag == "a":
            href = (c.attributes.get("href") or "").strip()
            txt = _inline(c).strip()
            out.append(f"[{txt}]({href})" if href and txt else txt)
        elif tag in ("strong", "b"):
            out.append(f"**{_inline(c).strip()}**")
        elif tag in ("em", "i"):
            out.append(f"*{_inline(c).strip()}*")
        elif tag == "br":
            out.append("\n")
        else:
            out.append(_inline(c))
    return "".join(out)


def _block(node, parts: list) -> None:
    tag = node.tag
    if tag in _DROP:
        return
    if tag in _HEADINGS:
        parts.append(f"\n{_HEADINGS[tag]} {_inline(node).strip()}\n")
        return
    if tag in ("p", "blockquote"):
        txt = _inline(node).strip()
        if txt:
            parts.append(("> " + txt if tag == "blockquote" else txt) + "\n")
        return
    if tag in ("ul", "ol"):
        for li in node.css("li"):
            t = _inline(li).strip()
            if t:
                parts.append(f"- {t}")
        parts.append("")
        return
    if tag in ("li", "a", "strong", "b", "em", "i", "span", "-text"):
        return  # handled inline by ancestors
    # generic container: recurse into children
    for c in node.iter(include_text=False):
        _block(c, parts)


def to_markdown(html: str) -> str:
    if not html or not _HAS:
        return ""
    tree = HTMLParser(html)
    body = tree.body or tree.root
    if body is None:
        return ""
    parts: list = []
    for c in body.iter(include_text=False):
        _block(c, parts)
    md = "\n".join(parts)
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
        "markdown": to_markdown(html),
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
