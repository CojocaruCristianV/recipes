"""recipes.json load / merge / atomic write, plus git commit + push with conflict retry."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .config import ROOT
from .models import RECIPE_FIELDS, Recipe

RECIPES = ROOT / "recipes.json"


class PublishError(Exception):
    pass


def load(path: Path = RECIPES) -> list[dict]:
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


def merge(existing: list[dict], new: list[Recipe]) -> list[dict]:
    """Newest first. A new recipe replaces an existing entry with the same url."""
    fresh = [r.to_json() for r in reversed(new)]
    new_urls = {r["url"] for r in fresh}
    rest = [r for r in existing if r.get("url") not in new_urls]
    out = fresh + rest
    for r in out:  # guard the format: never write extra fields or a missing url
        if set(r) != set(RECIPE_FIELDS) or not r.get("url"):
            raise ValueError(f"Invalid recipe entry for {r.get('url')!r}")
    return out


def write_atomic(data: list[dict], path: Path = RECIPES) -> None:
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


def publish(new: list[Recipe], max_attempts: int = 5) -> bool:
    """Merge new recipes into recipes.json, commit and push. Returns True if a commit was pushed."""
    if not new:
        return False
    rel = RECIPES.relative_to(ROOT).as_posix()
    for attempt in range(max_attempts):
        _git("pull", "--rebase", "--autostash", "-q")
        current = load()
        merged = merge(current, new)
        if merged == current:
            return False
        added = len(urls(merged) - urls(current)) or len(new)
        write_atomic(merged)
        _git("add", rel)
        _git("commit", "-q", "-m", f"Add {added} recipes")
        if _git("push", "-q", check=False).returncode == 0:
            return True
        # Rejected (someone pushed meanwhile): undo our commit, restore the file, re-merge.
        _git("reset", "-q", "--soft", "HEAD~1")
        _git("restore", "--staged", "--worktree", "--source=HEAD", rel)
        time.sleep(2 * attempt + 1)
    raise PublishError(f"Push rejected {max_attempts} times")
