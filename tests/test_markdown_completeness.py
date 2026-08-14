"""The markdown must carry every word a reader can see — no tag allowlist, no silent drops.

Fixture is designidentity.com.au: a Webflow site whose footer is a plain `<div class=
"nds-footer-wrapper">`, not a `<footer>`. `footerHtml` is legitimately null for it, so the
markdown is the only place that content can come from. Before the walker rewrite the markdown
dropped it too — every bare-`<div>` label, every `<a>`-wrapping-`<div>` contact row — because
only h1-h6/p/blockquote/ul/ol had a branch.

    python -m pytest tests/ -q      (or: python tests/test_markdown_completeness.py)
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.shape import to_markdown  # noqa: E402

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "divfooter_designidentity.html"

# Footer text that lives in bare divs / block-wrapping anchors. All of it was missing before.
FOOTER_TEXT = [
    "Services", "Company", "Contact Information",
    "(02) 8339 0130", "bookings@designidentity.com.au",
    "Ralph St", "Alexandria NSW 2015",
    "View Brochure", "Get In Touch", "Book now", "Contact Us",
    "Gadigal", "Design Identity Australia Pty Ltd",
]

# Body text the old converter already emitted — none of it may regress.
BODY_TEXT = [
    "Next-generation", "Production | AI | Photography | Videography",
    "Great visuals are visuals well captured", "Fully equipped Alexandria studio",
    "Lacoste", "Speedo", "Reebok", "Guess",
    "AI-generated or traditional content", "Book Your Studio Session Today!",
]


def _md(mode="inline"):
    return to_markdown(FIXTURE.read_text(), mode)


def test_footer_in_a_div_reaches_markdown():
    md = _md()
    missing = [s for s in FOOTER_TEXT if s not in md]
    assert not missing, f"visible footer text absent from markdown: {missing}"


def test_no_regression_on_body_text():
    md = _md()
    missing = [s for s in BODY_TEXT if s not in md]
    assert not missing, f"previously-emitted text lost: {missing}"


def test_link_modes_still_differ():
    inline, text, strip = _md("inline"), _md("text"), _md("strip")
    assert "[Book now](/contact)" in inline
    assert "Book now" in text and "](/contact)" not in text
    assert "Book now" not in strip          # block-wrapping anchors strip too


def test_invisible_content_stays_out():
    md = _md()
    for junk in ("var ", "gtm.start", "@media", "display:none"):
        assert junk not in md, f"non-rendered content leaked into markdown: {junk!r}"


if __name__ == "__main__":
    for fn in (test_footer_in_a_div_reaches_markdown, test_no_regression_on_body_text,
               test_link_modes_still_differ, test_invisible_content_stays_out):
        fn()
        print("ok  ", fn.__name__)
