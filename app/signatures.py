"""Deterministic block-page fingerprints. Pure string matching — no LLM.

Extend BLOCK_SIGNATURES as you meet new walls in production. High precision is the
goal: a false positive here causes needless (paid) escalation.
"""
from __future__ import annotations

# (name, [lowercase substrings]) — any hit flags a block/challenge page.
BLOCK_SIGNATURES: list[tuple[str, list[str]]] = [
    ("cloudflare_challenge", [
        "just a moment", "cf-chl", "cf_chl", "checking your browser",
        "cf-browser-verification", "_cf_chl_opt",
    ]),
    ("cloudflare_block", ["attention required", "cloudflare ray id", "error 1020"]),
    ("datadome", ["datadome", "geo.captcha-delivery.com", "dd_cookie"]),
    ("imperva_incapsula", [
        "pardon our interruption", "_incapsula_", "incapsula incident",
        "/_incapsula_resource",
    ]),
    ("akamai", ["errors.edgesuite.net", "reference #18."]),
    ("perimeterx", ["px-captcha", "perimeterx", "captcha.px-cloud", "_pxhd"]),
    ("turnstile", ["challenges.cloudflare.com/turnstile", "cf-turnstile"]),
    ("hcaptcha", ["hcaptcha.com/1/api.js", "h-captcha"]),
    ("recaptcha", ["www.google.com/recaptcha", "g-recaptcha", "grecaptcha.render"]),
    ("generic_denied", [
        "access to this page has been denied", "you have been blocked",
        "are you a robot", "verify you are a human", "access denied",
    ]),
]

# Captcha-family signatures: an IP swap (L3) will NOT fix these — they need a
# challenge solver (L2C/Byparr). Used by the escalator for smart routing.
CAPTCHA_SIGNATURES = {"turnstile", "hcaptcha", "recaptcha", "datadome", "perimeterx"}

# Resource URL patterns blocked at browser tiers — load HTML + JS only.
BLOCKED_URL_PATTERNS = [
    "*.css", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.mp4", "*.webm", "*.mp3", "*.avi", "*.mov",
    "*.pdf", "*google-analytics*", "*googletagmanager*", "*doubleclick*",
    "*facebook.com/tr*", "*hotjar*", "*segment.io*",
]


def find_block_signature(html_lower: str) -> str | None:
    for name, needles in BLOCK_SIGNATURES:
        for n in needles:
            if n in html_lower:
                return name
    return None
