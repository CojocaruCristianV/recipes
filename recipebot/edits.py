"""Changes requested from the website: edit and re-read issues.

The site opens an issue whose body ends with a ```json block:

  edit:   {"recipebot": "edit", "v": 1, "url": "<canonical url>",
           "base": {<recipe as shown when editing started>},
           "new":  {"name", "cuisine", "type", "time", "ingredients", "steps", "reviewed"}}
  reread: {"recipebot": "reread", "v": 1, "url": "<canonical url>"}

This module parses and validates that block, checks that the recipe did not change since
the edit started, builds the updated record and describes the changes for the issue comment.
It never calls Gemini, so edits still apply when Gemini is busy or out of quota.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter

from . import clean, urls
from .models import RECIPE_FIELDS

KINDS = ("edit", "reread")
# Fields compared by the conflict check and shown in change summaries.
EDITABLE = ("name", "cuisine", "type", "time", "ingredients", "steps", "needs_review")
MAX_ITEMS = 100
MAX_ITEM_LEN = 500
MAX_FIELD_LEN = 120

_JSON_BLOCK_RE = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)


class EditError(Exception):
    """The request cannot be applied. The message is shown in the issue comment."""


class Conflict(EditError):
    """The recipe changed after the edit was started."""


CONFLICT_TEXT = (
    "This recipe changed since you started editing (for example by another edit or a "
    "re-read), so nothing was overwritten. Reload the recipe page and make the edit again."
)


def payload(body: str | None) -> dict | None:
    """The last ```json block in an issue body that carries a "recipebot" kind, or None."""
    for block in reversed(_JSON_BLOCK_RE.findall(body or "")):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("recipebot") in KINDS:
            return data
    return None


def target_url(data: dict | None, fallback: str = "") -> str:
    """Canonical URL of the recipe the request is about."""
    raw = (data or {}).get("url") or fallback
    try:
        return urls.canonical(str(raw or ""))
    except ValueError:
        raise EditError("The request does not name a valid recipe link") from None


# ---------------------------------------------------------------------- comparing


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _l(v) -> list[str]:
    return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def view(r: dict) -> dict:
    """The editable fields of a record, normalized the same way the website reads them."""
    return {
        "name": _s(r.get("name")), "cuisine": _s(r.get("cuisine")), "type": _s(r.get("type")),
        "time": _s(r.get("time")), "ingredients": _l(r.get("ingredients")), "steps": _l(r.get("steps")),
        "needs_review": r.get("needs_review") is True,
    }


def check_base(current: dict, data: dict) -> None:
    """Raise Conflict unless the recipe still looks exactly as it did when editing started."""
    base = data.get("base")
    if not isinstance(base, dict):
        raise EditError("The edit has no starting point. Reload the recipe page and edit again.")
    if view(base) != view(current):
        raise Conflict(CONFLICT_TEXT)


# ---------------------------------------------------------------------- building


def _text(new: dict, key: str, label: str, required: bool = False) -> str:
    v = new.get(key, "")
    if not isinstance(v, str):
        raise EditError(f"{label} must be text")
    s = clean.clean_edit(v)
    if len(s) > MAX_FIELD_LEN:
        raise EditError(f"{label} is longer than {MAX_FIELD_LEN} characters")
    if required and not s:
        raise EditError(f"{label} cannot be empty")
    return s


def _items(v, label: str, numbering: bool) -> list[str]:
    if not isinstance(v, list):
        raise EditError(f"The {label}s are missing from the edit")
    out: list[str] = []
    for x in v:
        if not isinstance(x, str):
            raise EditError(f"Every {label} must be text")
        s = clean.clean_edit(x, numbering=numbering)
        if not s:
            continue  # empty boxes are dropped
        if len(s) > MAX_ITEM_LEN:
            raise EditError(f'The {label} "{s[:40]}..." is longer than {MAX_ITEM_LEN} characters')
        out.append(s)
    if not out:
        raise EditError(f"A recipe needs at least one {label}")
    if len(out) > MAX_ITEMS:
        raise EditError(f"A recipe can have at most {MAX_ITEMS} {label}s")
    return out


def build_edit(current: dict, data: dict, cuisines: list[str], types_: list[str]) -> tuple[dict, list[str]]:
    """Return (updated record, warnings). The link and the added date always stay."""
    new = data.get("new")
    if not isinstance(new, dict):
        raise EditError("The edit details are missing")
    if data.get("v") != 1:
        raise EditError("This edit was made by a newer version of the site. Reload the page and edit again.")
    ingredients = _items(new.get("ingredients"), "ingredient", numbering=False)
    steps = _items(new.get("steps"), "step", numbering=True)
    tbd = clean.has_tbd(ingredients) or clean.has_tbd(steps)
    warnings: list[str] = []
    if new.get("reviewed") is True:
        needs_review = False
        if tbd:
            warnings.append("Marked as reviewed, but some lines still contain `TBD:`.")
    else:
        # Unchanged unless the edit leaves a TBD gap, which always needs review.
        needs_review = view(current)["needs_review"] or tbd
    record = {k: current.get(k) for k in RECIPE_FIELDS}
    record.update(
        name=_text(new, "name", "Name", required=True),
        cuisine=clean.normalize_category(_text(new, "cuisine", "Cuisine"), cuisines, "Various"),
        type=clean.normalize_category(_text(new, "type", "Type"), types_, "Other"),
        time=_text(new, "time", "Time") or "TBD",
        ingredients=ingredients,
        steps=steps,
        needs_review=needs_review,
    )
    return record, warnings


# ---------------------------------------------------------------------- describing

_ARROW = " \u2192 "


def _q(s: str, n: int = 90) -> str:
    s = s if len(s) <= n else s[: n - 3] + "..."
    return '"' + s.replace("\n", " ") + '"'


def _split(seq: list[str], common: Counter) -> tuple[list[str], list[str]]:
    """(lines that also exist in the other version, the rest), both in original order."""
    left = Counter(common)
    kept, rest = [], []
    for x in seq:
        if left[x] > 0:
            left[x] -= 1
            kept.append(x)
        else:
            rest.append(x)
    return kept, rest


def list_changes(old: list[str], new: list[str]) -> list[str]:
    """Changed, added, removed and moved lines between two versions of a list.

    Identical lines are matched first (a line that only changed place is "moved"); of the
    remaining lines, similar pairs are "changed" and the rest are "added" / "removed".
    """
    common = Counter(old) & Counter(new)
    kept_old, removed = _split(old, common)
    kept_new, added = _split(new, common)
    # Kept lines outside the longest common ordering are the ones that changed place.
    sm = difflib.SequenceMatcher(a=kept_old, b=kept_new, autojunk=False)
    moved: list[str] = []
    pos = 0
    for blk in sm.get_matching_blocks():
        moved += kept_new[pos:blk.b]
        pos = blk.b + blk.size
    changed: list[tuple[str, str]] = []
    for a in list(removed):
        best, score = None, 0.5
        for b in added:
            r = difflib.SequenceMatcher(a=a.lower(), b=b.lower(), autojunk=False).ratio()
            if r > score:
                best, score = b, r
        if best is not None:
            changed.append((a, best))
            removed.remove(a)
            added.remove(best)
    out = [f"changed {_q(a)}{_ARROW}{_q(b)}" for a, b in changed]
    out += [f"added {_q(x)}" for x in added]
    out += [f"removed {_q(x)}" for x in removed]
    out += [f"moved {_q(x)}" for x in moved]
    if not out and old != new:
        out.append("reordered")
    return out


def changes(before: dict, after: dict) -> list[str]:
    """Markdown bullet lines describing what changed between two records."""
    a, b = view(before), view(after)
    out: list[str] = []
    for key, label in (("name", "Name"), ("cuisine", "Cuisine"), ("type", "Type"), ("time", "Time")):
        if a[key] != b[key]:
            out.append(f"- {label}: {a[key] or '(empty)'}{_ARROW}{b[key] or '(empty)'}")
    for key, label in (("ingredients", "Ingredients"), ("steps", "Steps")):
        lines = list_changes(a[key], b[key])
        if lines:
            out.append(f"- {label}:")
            out += [f"  - {x}" for x in lines]
    if a["needs_review"] != b["needs_review"]:
        out.append("- Marked as reviewed" if not b["needs_review"] else "- Now flagged for review")
    return out
