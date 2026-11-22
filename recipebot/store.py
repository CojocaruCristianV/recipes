"""recipes.json load / merge / atomic write, plus git commit + push with conflict retry."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import ROOT
from .models import RECIPE_FIELDS, Recipe

RECIPES = ROOT / "recipes.json"


class PublishError(Exception):
    pass


class Missing(LookupError):
    """The recipe an update is about is no longer in recipes.json."""


@dataclass
class Update:
    """An in-place change to one recipe (website edit or re-read).

    apply(current record) returns the new record. It runs again on the freshly pulled
    file before every push attempt, so it must re-check anything it depends on (an edit
    re-checks that the recipe still matches what the site showed) and may raise to refuse.
    """
    url: str
    apply: Callable[[dict], dict]


@dataclass
class Published:
    pushed: bool = False
    commit: str = ""
    # One entry per update, in order: (before, after) when applied, else None.
    applied: list = field(default_factory=list)
    # One entry per update, in order: the exception that refused it, else None.
    errors: list = field(default_factory=list)


def load(path: Path | None = None) -> list[dict]:
    path = path or RECIPES
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    data = json.loads(text) if text else []
    if not isinstance(data, list):
        raise ValueError("recipes.json must contain a JSON array")
    return data


def distinct(recipes: list[dict], key: str) -> list[str]:
    seen: dict[str, str] = {}
    for r in recipes:
        v = str(r.get(key) or "").strip()
        if v and v.lower() not in seen:
            seen[v.lower()] = v
    return sorted(seen.values(), key=str.lower)


def urls(recipes: list[dict]) -> set[str]:
    return {r.get("url", "") for r in recipes if r.get("url")}


def _check(records: list[dict]) -> None:
    for r in records:  # guard the format: never write extra fields or a missing url
        if set(r) != set(RECIPE_FIELDS) or not r.get("url"):
            raise ValueError(f"Invalid recipe entry for {r.get('url')!r}")


def merge(existing: list[dict], new: list[Recipe]) -> list[dict]:
    """Newest first. A new recipe replaces an existing entry with the same url."""
    fresh = [r.to_json() for r in reversed(new)]
    new_urls = {r["url"] for r in fresh}
    rest = [r for r in existing if r.get("url") not in new_urls]
    out = fresh + rest
    _check(out)
    return out


def apply_updates(records: list[dict], updates: list[Update]) -> tuple[list[dict], list, list]:
    """Apply updates in order, each replacing its recipe at the same position.

    Later updates see the result of earlier ones. Returns (records, applied, errors).
    """
    out = [dict(r) for r in records]
    index = {r.get("url"): i for i, r in enumerate(out)}
    applied: list = []
    errors: list = []
    for u in updates:
        i = index.get(u.url)
        if i is None:
            applied.append(None)
            errors.append(Missing("This recipe is no longer in the library."))
            continue
        before = out[i]
        try:
            after = Recipe.model_validate(u.apply(dict(before))).to_json()
        except Exception as e:  # noqa: BLE001 - reported per update, never blocks the others
            applied.append(None)
            errors.append(e)
            continue
        if after["url"] != before.get("url"):
            applied.append(None)
            errors.append(ValueError("An update may not change the recipe link"))
            continue
        out[i] = after
        applied.append((before, after))
        errors.append(None)
    _check(out)
    return out, applied, errors


def commit_message(current: list[dict], merged: list[dict], new: list[Recipe], applied: list) -> str:
    added = len(urls(merged) - urls(current)) or len(new)
    changed = [a for b, a in (x for x in applied if x) if a != b]
    n = len(changed)
    if added and n:
        return f"Add {added}, update {n} recipe{'s' if added + n != 1 else ''}"
    if added:
        return f"Add {added} recipe{'s' if added != 1 else ''}"
    if n == 1:
        return f"Update: {changed[0]['name']}"
    return f"Update {n} recipes"


def write_atomic(data: list[dict], path: Path | None = None) -> None:
    path = path or RECIPES
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".recipes.json.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise PublishError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r


def publish(new: list[Recipe], updates: list[Update] | None = None, max_attempts: int = 5) -> Published:
    """Merge new recipes and apply updates to recipes.json, commit and push.

    Every attempt starts from the freshly pulled file, so updates are re-applied (and
    re-checked) against whatever is on the remote at that moment.
    """
    updates = list(updates or [])
    if not new and not updates:
        return Published()
    rel = RECIPES.relative_to(ROOT).as_posix()
    for attempt in range(max_attempts):
        _git("pull", "--rebase", "--autostash", "-q")
        current = load()
        merged, applied, errors = apply_updates(merge(current, new), updates)
        res = Published(applied=applied, errors=errors)
        if merged == current:
            return res
        write_atomic(merged)
        _git("add", rel)
        _git("commit", "-q", "-m", commit_message(current, merged, new, applied))
        if _git("push", "-q", check=False).returncode == 0:
            res.pushed = True
            res.commit = _git("rev-parse", "HEAD").stdout.strip()
            return res
        # Rejected (someone pushed meanwhile): undo our commit, restore the file, re-merge.
        _git("reset", "-q", "--soft", "HEAD~1")
        _git("restore", "--staged", "--worktree", "--source=HEAD", rel)
        time.sleep(2 * attempt + 1)
    raise PublishError(f"Push rejected {max_attempts} times")
