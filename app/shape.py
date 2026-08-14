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

# Nothing inside these ever reaches the reader's eye, so nothing inside them reaches the markdown.
_INVISIBLE = _DROP | {
    "head", "title", "meta", "link", "base", "iframe", "object", "param", "source", "track",
    "canvas", "map", "area", "audio", "video", "picture", "select", "option", "datalist", "dialog",
}

# Inline-level tags: their text belongs on the enclosing block's line, not a line of its own.
_INLINE_TAGS = {
    "a", "abbr", "b", "bdi", "bdo", "big", "cite", "code", "data", "del", "dfn", "em", "font",
    "i", "img", "ins", "kbd", "mark", "nobr", "q", "rp", "rt", "ruby", "s", "samp", "small",
    "span", "strong", "sub", "sup", "time", "u", "var", "wbr", "br", "-text",
}

# After these, a blank line keeps blocks from running together. List items stay out — they read
# as one list, not one paragraph each.
_SPACE_AFTER = {"p", "blockquote", "tr", "section", "article", "footer",
                "header", "nav", "aside", "form", "figure", "figcaption", "main", "address"}

# Markup often packs nav/menu anchors with no whitespace between them (`</a><a>`), which the
# browser still spaces out via CSS. Without a separator they concatenate into one junk token.
_SEPARATE = {"a", "button"}
_NO_SEPARATE_AFTER = set("([{“‘\"'/–—-")


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
def _hidden(node) -> bool:
    """Markup-level invisibility only. Stylesheet-driven hiding isn't knowable without a layout
    engine, so we honour what the tag itself declares and nothing more."""
    a = node.attributes
    if "hidden" in a:
        return True
    if (a.get("aria-hidden") or "").lower() == "true":
        return True
    style = (a.get("style") or "").lower().replace(" ", "")
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if node.tag == "input" and (a.get("type") or "").lower() == "hidden":
        return True
    return False


def _inline_node(c, links: str = LINKS_INLINE) -> str:
    """Render ONE inline node, itself included.

    Split out of `_inline` because `_inline(node)` only ever formats a node's *descendants* —
    hand it an `<a>` and you get the bare label, no `[..](href)` and no honouring of `strip`.
    The block walker hands it single nodes constantly, so the dispatch has to live here."""
    tag = c.tag
    if tag == "-text":
        return c.text(deep=False) or ""
    if tag in _INVISIBLE or _hidden(c):
        return ""
    if tag == "a":
        if links == LINKS_STRIP:          # anchor contributes nothing — not even its label
            return ""
        href = (c.attributes.get("href") or "").strip()
        txt = _inline(c, links).strip()
        return f"[{txt}]({href})" if links == LINKS_INLINE and href and txt else txt
    if tag in ("strong", "b"):
        return f"**{_inline(c, links).strip()}**"
    if tag in ("em", "i"):
        return f"*{_inline(c, links).strip()}*"
    if tag == "br":
        return "\n"
    return _inline(c, links)


def _inline(node, links: str = LINKS_INLINE) -> str:
    return "".join(_inline_node(c, links) for c in node.iter(include_text=True))


def _is_block(node) -> bool:
    """Block unless it's an inline tag holding only inline content.

    The second half is what saves `<a><div>(02) 8339 0130</div></a>` — legal HTML5, and the
    shape every button/contact-row in a div-built footer takes. Treated as inline, its text
    would be buffered onto a parent line that may never be flushed."""
    if node.tag not in _INLINE_TAGS:
        return True
    for d in node.iter(include_text=False):
        if d.tag not in _INLINE_TAGS and d.tag not in _INVISIBLE:
            return True
    return False


class _Bullet:
    """A list marker waiting for a line to attach to. The first line emitted anywhere in the
    `<li>`'s subtree takes the bullet; every later line takes the matching indent, so an item
    whose text sits several divs deep still reads as one item."""

    def __init__(self, indent: str = ""):
        self.indent, self.used = indent, False

    def take(self) -> str:
        if self.used:
            return self.indent + "  "
        self.used = True
        return self.indent + "- "


def _flush(buf: list, parts: list, bullet=None) -> None:
    """Emit the buffered inline run as one line."""
    if not buf:
        return
    txt = "".join(buf)
    buf.clear()
    txt = re.sub(r"[ \t]*\n[ \t]*", "\n", txt)   # <br> runs
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    txt = txt.strip()
    if not txt:
        return
    if bullet is None:
        parts.append(txt)
        return
    lines = txt.split("\n")
    parts.append(bullet.take() + lines[0])
    for extra in lines[1:]:
        parts.append(bullet.take() + extra)


def _table(node, parts: list, links: str) -> None:
    """Pipe table. Cells were dropped wholesale before — every `td` fell through the allowlist."""
    grid = []
    for row in node.css("tr"):
        cells = [_inline(c, links).strip().replace("|", "\\|").replace("\n", " ")
                 for c in row.iter(include_text=False) if c.tag in ("td", "th")]
        if any(cells):
            grid.append(cells)
    if not grid:
        return
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    parts.append("| " + " | ".join(grid[0]) + " |")
    parts.append("| " + " | ".join(["---"] * width) + " |")
    for r in grid[1:]:
        parts.append("| " + " | ".join(r) + " |")
    parts.append("")


def _block(node, parts: list, links: str = LINKS_INLINE, depth: int = 0, bullet=None) -> None:
    """Walk one block node. Text is emitted by default; only proven-invisible nodes are skipped.

    Inline children accumulate into a buffer that flushes as a single line whenever a block child
    interrupts it — that's what puts a bare `<div>Services</div>` or a `<a>Book now</a>` into the
    output, neither of which the old tag allowlist had a branch for."""
    tag = node.tag
    if tag in _INVISIBLE or _hidden(node):
        return
    if tag == "a" and links == LINKS_STRIP:
        return          # block-wrapping anchor — `strip` means gone, same as the inline case
    if tag in _HEADINGS:
        txt = _inline(node, links).strip()
        if txt:
            parts.append(f"\n{_HEADINGS[tag]} {txt}\n")
        return
    if tag == "table":
        _table(node, parts, links)
        return
    if tag in ("ul", "ol", "dl"):
        for c in node.iter(include_text=False):
            if c.tag in ("li", "dt", "dd"):
                _block(c, parts, links, depth + 1)
            else:                                    # stray wrapper between list and items
                _block(c, parts, links, depth, bullet)
        parts.append("")
        return

    if tag in ("li", "dd", "dt") and depth:
        bullet = _Bullet("  " * (depth - 1))
    start = len(parts)

    buf: list = []
    for c in node.iter(include_text=True):
        if c.tag == "-text":
            buf.append(c.text(deep=False) or "")
        elif c.tag in _INVISIBLE or _hidden(c):
            continue
        elif not _is_block(c):
            if (c.tag in _SEPARATE and buf and buf[-1]
                    and buf[-1][-1] not in _NO_SEPARATE_AFTER and not buf[-1][-1].isspace()):
                buf.append(" ")
            buf.append(_inline_node(c, links))
        else:
            _flush(buf, parts, bullet)
            _block(c, parts, links, depth, bullet)
    _flush(buf, parts, bullet)

    if tag == "blockquote":
        for i in range(start, len(parts)):
            if parts[i].strip():
                parts[i] = "> " + parts[i]
    if tag in _SPACE_AFTER:
        parts.append("")


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
    _block(body, parts, links)
    md = "\n".join(parts)
    if links == LINKS_STRIP:
        # A removed anchor leaves a hole mid-sentence: "read the  ." Close the gap so the prose
        # that survives reads cleanly (run-together spaces, then space-before-punctuation).
        # Leading indent is skipped — it carries nested-list depth, not a hole.
        def _close_gaps(line: str) -> str:
            lead = line[:len(line) - len(line.lstrip(" \t"))]
            rest = re.sub(r"[ \t]{2,}", " ", line[len(lead):])
            rest = re.sub(r" +([,.;:!?)\]])", r"\1", rest)
            return lead + re.sub(r"([(\[]) +", r"\1", rest)

        md = "\n".join(_close_gaps(ln) for ln in md.split("\n"))
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
