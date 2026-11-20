"""Text cleanup for list items and normalization of cuisine/type values."""
from __future__ import annotations

import re
import unicodedata

# Keycap sequences like "1" + U+FE0F + U+20E3 used as step numbers.
_KEYCAP_RE = re.compile(r"^\s*\d+\uFE0F?\u20E3\s*")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U000020E3"
    "\U0001F3FB-\U0001F3FF"
    "]+"
)
_HASHTAG_RE = re.compile(r"(?<![\w&])#[\w\u00C0-\u024F]+")
# Leading bullets. "~" is intentionally not a bullet: it marks estimated amounts.
_BULLET_RE = re.compile(
    r"^[\s\-\*\u2022\u2023\u2043\u2219\u00B7\u25AA\u25AB\u25CF\u25CB\u25E6\u25B6\u25BA"
    r"\u2013\u2014\u2192\u27A4\u27A2\u2714\u2713\u2705]+"
)
_NUMBERING_RE = re.compile(
    r"^(?:step\s*\d+\s*[.):\-]?|\d+\s*[.)\]:](?=\s|$)|\(\d+\)|[a-z]\)(?=\s))\s*", re.I
)
_HEADER_RE = re.compile(
    r"^(ingredients?|method|steps?|instructions?|directions?|preparation|recipe)\s*:?$", re.I
)
_WS_RE = re.compile(r"\s+")


def clean_line(text: str, numbering: bool = False) -> str:
    s = unicodedata.normalize("NFC", text or "")
    if numbering:
        s = _KEYCAP_RE.sub("", s)
    s = _EMOJI_RE.sub(" ", s)
    s = _HASHTAG_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    s = _BULLET_RE.sub("", s).strip()
    if numbering:
        s = _NUMBERING_RE.sub("", s).strip()
        s = _BULLET_RE.sub("", s).strip()
    return s


def clean_lines(items: list[str] | None, numbering: bool = False) -> list[str]:
    out: list[str] = []
    for item in items or []:
        for part in str(item).splitlines():
            line = clean_line(part, numbering=numbering)
            if line and not _HEADER_RE.match(line):
                out.append(line)
    return out


def has_tbd(items: list[str]) -> bool:
    return any("TBD:" in s for s in items)


def title_case(value: str) -> str:
    words = []
    for word in _WS_RE.sub(" ", (value or "").strip()).split(" "):
        parts = []
        for p in word.split("-"):
            if p.isupper() and 1 < len(p) <= 4:
                parts.append(p)  # keep acronyms like BBQ
            else:
                parts.append(p[:1].upper() + p[1:].lower())
        words.append("-".join(parts))
    return " ".join(w for w in words if w)


def _key(value: str) -> str:
    k = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return k[:-1] if k.endswith("s") and len(k) > 3 else k


def normalize_category(value: str, existing: list[str], fallback: str) -> str:
    """Trim, Title Case, and map case-insensitive / plural matches to an existing value."""
    v = title_case(clean_line(value)) or fallback
    k = _key(v)
    for e in existing:
        if _key(e) == k:
            return e
    return v
