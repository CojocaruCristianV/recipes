"""Debug helper: save the raw Apify output for a post and its creator's profile.

  python -m recipebot --probe <instagram post url>

Runs the configured Instagram actor three times (post, comments, creator profile) and
writes each raw dataset to .probe/<shortcode>_<kind>.json (git-ignored). Nothing is
committed and no issue is touched. Used to confirm which fields the actor returns
before code depends on them. Costs a few Apify runs (well under 0.01 USD each).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

from . import urls
from .config import ROOT

PROBE_DIR = ROOT / ".probe"


def _run(client, actor: str, run_input: dict, timeout_s: int) -> list[dict]:
    run = client.actor(actor).call(run_input=run_input, run_timeout=timedelta(seconds=timeout_s), logger=None)
    if run is None:
        raise RuntimeError("actor run did not return")
    dataset_id = getattr(run, "default_dataset_id", None)
    if not dataset_id:
        raise RuntimeError(f"no dataset (status {getattr(run, 'status', '?')})")
    return [dict(i) for i in (client.dataset(dataset_id).list_items(limit=50, clean=True).items or [])]


def _summary(items: list[dict]) -> str:
    """Top-level keys of the first item, so the console shows what came back."""
    if not items:
        return "0 items"
    keys = sorted(items[0].keys())
    return f"{len(items)} item(s); keys: {', '.join(keys)}"


def probe(url: str, cfg: dict) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        print("APIFY_TOKEN not set (add it to .env)", file=sys.stderr)
        return 2
    from apify_client import ApifyClient

    canonical = urls.resolve(url)
    code = urls.shortcode(canonical)
    fcfg = cfg["fetch"]
    actor = fcfg["apify_actor"]
    timeout_s = int(fcfg["apify_timeout_s"])
    client = ApifyClient(token)
    PROBE_DIR.mkdir(exist_ok=True)

    jobs = [
        ("post", {"directUrls": [canonical], "resultsType": "posts", "resultsLimit": 1, "addParentData": False}),
        ("comments", {"directUrls": [canonical], "resultsType": "comments", "resultsLimit": 50}),
    ]
    owner = ""
    for kind, run_input in jobs:
        try:
            items = _run(client, actor, run_input, timeout_s)
        except Exception as e:  # noqa: BLE001 - debug tool, report and continue
            print(f"{kind}: FAILED {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            continue
        path = PROBE_DIR / f"{code}_{kind}.json"
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"{kind}: {_summary(items)} -> {path.relative_to(ROOT)}", file=sys.stderr)
        if kind == "post" and items:
            owner = str(items[0].get("ownerUsername") or "")

    if owner:
        profile_url = f"https://www.instagram.com/{owner}/"
        try:
            items = _run(client, actor, {"directUrls": [profile_url], "resultsType": "details", "resultsLimit": 1},
                         timeout_s)
            path = PROBE_DIR / f"{code}_profile.json"
            path.write_text(json.dumps(items, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"profile (@{owner}): {_summary(items)} -> {path.relative_to(ROOT)}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"profile: FAILED {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
    else:
        print("profile: skipped (no ownerUsername in the post result)", file=sys.stderr)
    return 0
