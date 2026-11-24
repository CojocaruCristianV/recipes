"""Fill in the Instagram username ("author") of recipes saved before that field existed.

  python -m recipebot --backfill-authors --dry-run   (show what would change, write nothing)
  python -m recipebot --backfill-authors             (commit and push recipes.json)

All posts are fetched in one Apify run (about 0.003 USD per post). Only recipes with an
empty author are touched, and only their author field changes. Because the change goes
through store.publish, every record also gets the newer fields (author, source) written
out, and a push conflict is retried on the freshly pulled file like any other bot run.
"""
from __future__ import annotations

from . import fetchers, store
from .fetchers import USERNAME_RE
from .processor import log, sanitize
from .urls import shortcode


def _owners(urls: list[str], cfg: dict) -> dict[str, str]:
    """Map Instagram shortcode -> owner username, from one Apify posts run."""
    run_input = {"directUrls": urls, "resultsType": "posts", "resultsLimit": 1}
    items = fetchers._apify_run(run_input, cfg, limit=len(urls) + 5)
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("shortCode") or "") or shortcode(str(it.get("url") or ""))
        owner = str(it.get("ownerUsername") or "")
        if code and USERNAME_RE.match(owner):
            out[code] = owner
    return out


def backfill_authors(cfg: dict, dry_run: bool = False) -> int:
    records = store.load()
    todo = [r for r in records if isinstance(r, dict) and r.get("url") and not r.get("author")]
    if not todo:
        log(status="backfill", msg="every recipe already has an author", recipes=len(records))
        return 0
    urls = [r["url"] for r in todo]
    log(status="backfill", msg=f"looking up {len(urls)} post owner(s) in one Apify run")
    try:
        owners = _owners(urls, cfg)
    except Exception as e:  # noqa: BLE001
        log(status="error", error=sanitize(f"Apify lookup failed: {type(e).__name__}: {e}"))
        return 1

    found = {u: owners[shortcode(u)] for u in urls if shortcode(u) in owners}
    for u in urls:
        name = next((r.get("name") for r in todo if r["url"] == u), "")
        log(status="backfill", url=u, name=name, author=found.get(u) or "(not found)")
    if not found:
        log(status="backfill", msg="no owners found; nothing to change")
        return 1
    if dry_run:
        log(status="backfill", msg=f"dry run: {len(found)} of {len(urls)} would be updated; nothing written")
        return 0

    def setter(owner: str):
        def apply(cur: dict) -> dict:
            # Re-checked on the freshly pulled file: never overwrite an author set meanwhile.
            if cur.get("author"):
                return cur
            return {**cur, "author": owner}
        return apply

    updates = [store.Update(url=u, apply=setter(o)) for u, o in found.items()]
    try:
        res = store.publish([], updates)
    except store.PublishError as e:
        log(status="error", error=sanitize(e))
        return 3
    changed = sum(1 for a in res.applied if a and a[0] != a[1])
    log(status="backfill", updated=changed, pushed=res.pushed, commit=res.commit[:12])
    return 0
