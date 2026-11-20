"""Instagram URL extraction and canonicalization.

Canonical form: https://www.instagram.com/p/<shortcode>/
/p/, /reel/, /reels/, /tv/ (optionally prefixed by a username) share the same
shortcode, so they all map to one canonical URL. Query strings are dropped.
"""
from __future__ import annotations

import re

import requests

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)

_URL_RE = re.compile(r"https?://(?:[a-z0-9-]+\.)?instagram\.com/[^\s<>\"')\]]+", re.I)
_SHARE_RE = re.compile(r"instagram\.com/share/", re.I)
_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})", re.I
)
_CANON_RE = re.compile(r"^https://www\.instagram\.com/p/([A-Za-z0-9_-]{5,})/$")


def extract_url(text: str | None) -> str | None:
    """Return the first instagram.com URL found in text, or None."""
    m = _URL_RE.search(text or "")
    return m.group(0) if m else None


def canonical(url: str) -> str:
    """Map a post/reel URL to its canonical form. Raises ValueError if not a post URL."""
    if _SHARE_RE.search(url or ""):
        raise ValueError("Share link must be resolved first")
    m = _SHORTCODE_RE.search(url or "")
    if not m:
        raise ValueError(f"Not an Instagram post/reel URL: {url[:120]}")
    return f"https://www.instagram.com/p/{m.group(1)}/"


def is_canonical(url: str) -> bool:
    return bool(_CANON_RE.match(url or ""))


def shortcode(url: str) -> str:
    return canonical(url).rstrip("/").rsplit("/", 1)[-1]


def resolve(url: str, timeout: int = 15) -> str:
    """Canonicalize, following instagram.com/share/... redirects when needed."""
    if not url:
        raise ValueError("Empty URL")
    if not _SHARE_RE.search(url):
        return canonical(url)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=timeout)
    except requests.RequestException as e:
        raise ValueError(f"Could not resolve share link: {type(e).__name__}") from None
    candidates = [r.url] + [h.headers.get("Location", "") for h in r.history]
    for cand in candidates:
        if cand and not _SHARE_RE.search(cand) and _SHORTCODE_RE.search(cand):
            return canonical(cand)
    raise ValueError("Share link did not resolve to a post/reel URL")
