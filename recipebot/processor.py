"""Queue processing: issue -> fetch -> Gemini -> Recipe -> publish -> issue updates."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from . import clean, fetchers, store, urls
from .gemini import Gemini, GeminiError, QuotaExceeded
from .models import Extraction, Recipe

STATUS_LABELS = [
    "queued", "retry", "processing", "done", "failed", "duplicate",
    "not-recipe", "needs-review", "needs-local",
]


class NotRecipe(Exception):
    pass


_SECRET_RE = re.compile(
    r"(apify_api_[A-Za-z0-9]+|AIza[0-9A-Za-z_\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|(?:token|key|sig|signature)=[^&\s]+)"
)


def sanitize(msg: str, limit: int = 300) -> str:
    return _SECRET_RE.sub("[redacted]", str(msg or "")).strip()[:limit]


def log(**fields) -> None:
    fields = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **fields}
    print(json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


@dataclass
class Item:
    url: str  # raw URL as shared
    issue: dict | None = None
    canonical: str = ""

    @property
    def number(self) -> int | None:
        return self.issue["number"] if self.issue else None

    @property
    def labels(self) -> list[str]:
        return [lb["name"] for lb in (self.issue or {}).get("labels", [])]


@dataclass
class Outcome:
    item: Item
    status: str
    recipe: Recipe | None = None
    source: str = ""
    fetcher: str = ""
    error: str = ""
    review_reason: str = ""


@dataclass
class Processor:
    cfg: dict
    dry_run: bool = False
    local: bool = False
    force: bool = False
    gh: object | None = None
    _gemini: Gemini | None = field(default=None, init=False)

    # ------------------------------------------------------------------ setup

    @property
    def gemini(self) -> Gemini:
        if self._gemini is None:
            self._gemini = Gemini(self.cfg)
        return self._gemini

    def today(self) -> str:
        return datetime.now(ZoneInfo(self.cfg["run"]["timezone"])).strftime("%Y-%m-%d")

    @staticmethod
    def item_from_issue(issue: dict) -> Item:
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        return Item(url=urls.extract_url(text) or "", issue=issue)

    # ------------------------------------------------------------------ issue helpers

    def _set(self, item: Item, add: list[str], close: str | None = None, comment: str | None = None) -> None:
        if self.dry_run or not item.issue or self.gh is None:
            return
        n = item.number
        try:
            if comment:
                self.gh.comment(n, comment)
            current = item.labels
            self.gh.relabel(n, add, [lb for lb in STATUS_LABELS if lb not in add], current)
            item.issue["labels"] = [{"name": lb} for lb in current if lb not in STATUS_LABELS or lb in add] + [
                {"name": lb} for lb in add if lb not in current
            ]
            if close:
                self.gh.close(n, close)
        except Exception as e:  # issue bookkeeping must never crash the run
            log(issue=n, status="issue-update-error", error=sanitize(e))

    # ------------------------------------------------------------------ extraction

    def _extract(self, post: fetchers.Post, cuisines: list[str], types_: list[str]) -> tuple[Extraction, str, bool, bool]:
        """Return (extraction, source, ingredients_from_caption, steps_from_caption)."""
        caption = (post.caption or "").strip()
        cap: Extraction | None = None
        if caption:
            cap = self.gemini.from_caption(caption, cuisines, types_)
        cap_ing = bool(cap and cap.caption_has_ingredients and cap.ingredients)
        cap_steps = bool(cap and cap.caption_has_steps and cap.steps)
        if cap_ing and cap_steps:
            return cap, "caption", True, True

        missing = [p for p, have in (("ingredients", cap_ing), ("steps", cap_steps)) if not have]
        if post.has_media:
            with tempfile.TemporaryDirectory(prefix="rb_media_") as tmp:
                media = post.download(Path(tmp))
                if media:
                    med = self.gemini.from_media(caption, media, missing, cuisines, types_)
                    for m in media:
                        m.path.unlink(missing_ok=True)
                    kind = "video" if any(m.mime.startswith("video/") for m in media) else "images"
                    if cap_ing or cap_steps:
                        assert cap is not None
                        med.ingredients = cap.ingredients if cap_ing else med.ingredients
                        med.steps = cap.steps if cap_steps else med.steps
                        med.is_recipe = True
                        source = f"caption + {kind}"
                    else:
                        source = kind
                    return med, source, cap_ing, cap_steps
        if cap is None:
            raise fetchers.FetchError("Post has no caption and no downloadable media")
        if not (cap.ingredients or cap.steps):
            raise NotRecipe("Caption has no ingredients or method and no media is available")
        # Partial caption, no media: keep what exists and flag the gap.
        cap.needs_review = True
        cap.review_reason = cap.review_reason or f"Missing {' and '.join(missing)}; media not available"
        return cap, "caption (partial)", cap_ing, cap_steps

    def _build(self, ex: Extraction, url: str, from_cap_ing: bool, from_cap_steps: bool,
               cuisines: list[str], types_: list[str]) -> Recipe:
        ingredients = clean.clean_lines(ex.ingredients, numbering=False)
        steps = clean.clean_lines(ex.steps, numbering=True)
        if not ex.is_recipe or (not ingredients and not steps):
            raise NotRecipe("No ingredients and no method found")
        needs_review = bool(ex.needs_review)
        if not ingredients:
            ingredients, needs_review = ["TBD: ingredients not shown"], True
        if not steps:
            steps, needs_review = ["TBD: method not shown"], True
        if clean.has_tbd(ingredients) or clean.has_tbd(steps):
            needs_review = True
        name = clean.clean_line(ex.name) or "Untitled Recipe"
        return Recipe(
            name=name,
            cuisine=clean.normalize_category(ex.cuisine, cuisines, "Various"),
            type=clean.normalize_category(ex.type, types_, "Other"),
            time=clean.clean_line(ex.time) or "TBD",
            ingredients=ingredients,
            steps=steps,
            url=url,  # always set in code, never by the model
            needs_review=needs_review,
            added=self.today(),
        )

    def process_one(self, item: Item, known: set[str], cuisines: list[str], types_: list[str]) -> Outcome:
        if not item.url:
            return Outcome(item, "failed", error="No instagram.com URL found in the issue")
        try:
            item.canonical = urls.resolve(item.url)
        except ValueError as e:
            return Outcome(item, "failed", error=sanitize(e))

        reprocess = self.force or "retry" in item.labels
        if item.canonical in known and not reprocess:
            return Outcome(item, "duplicate")
        known.add(item.canonical)

        self._set(item, ["processing"])
        order = self.cfg["fetch"]["local_order" if self.local else "order"]
        last_err = ""
        for attempt in range(2):  # one retry for ordinary errors
            fetcher = ""
            try:
                post = fetchers.fetch(item.canonical, self.cfg, order)
                fetcher = post.fetcher
                ex, source, ci, cs = self._extract(post, cuisines, types_)
                recipe = self._build(ex, item.canonical, ci, cs, cuisines, types_)
                return Outcome(item, "ok", recipe, source, fetcher, review_reason=ex.review_reason)
            except QuotaExceeded as e:
                return Outcome(item, "quota", error=sanitize(e), fetcher=fetcher)
            except fetchers.AllBlocked as e:
                return Outcome(item, "needs-local", error=sanitize(e))
            except NotRecipe as e:
                return Outcome(item, "not-recipe", error=sanitize(e), fetcher=fetcher)
            except (fetchers.FetchError, GeminiError, ValidationError, OSError, RuntimeError, ValueError) as e:
                last_err = sanitize(f"{type(e).__name__}: {e}")
                if isinstance(e, GeminiError) and "not set" in str(e):
                    raise
            except Exception as e:  # noqa: BLE001 - last-resort guard per item
                last_err = sanitize(f"{type(e).__name__}: {e}")
            if attempt == 0:
                time.sleep(3)
        return Outcome(item, "failed", error=last_err)

    # ------------------------------------------------------------------ run

    def run(self, items: list[Item]) -> dict:
        start = time.monotonic()
        existing = store.load()
        known = store.urls(existing)
        cuisines = store.distinct(existing, "cuisine")
        types_ = store.distinct(existing, "type")
        budget = int(self.cfg["run"]["run_budget_s"])
        limit = int(self.cfg["run"]["max_items_per_run"])

        outcomes: list[Outcome] = []
        stopped = ""
        for idx, item in enumerate(items):
            if idx >= limit or time.monotonic() - start > budget:
                stopped = "budget"
                break
            t0 = time.monotonic()
            out = self.process_one(item, known, cuisines, types_)
            outcomes.append(out)
            log(issue=item.number, url=item.canonical or item.url, status=out.status,
                source=out.source, fetcher=out.fetcher, ms=int((time.monotonic() - t0) * 1000),
                error=out.error or None)
            if out.recipe:
                for value, pool in ((out.recipe.cuisine, cuisines), (out.recipe.type, types_)):
                    if value not in pool:
                        pool.append(value)
            self._apply_immediate(out)
            if out.status == "quota":
                stopped = "quota"
                break

        recipes = [o.recipe for o in outcomes if o.status == "ok" and o.recipe]
        published = False
        if self.dry_run:
            print(json.dumps([r.to_json() for r in recipes], ensure_ascii=False, indent=2))
        elif recipes:
            try:
                published = store.publish(recipes, int(self.cfg["run"]["max_push_attempts"]))
            except store.PublishError as e:
                log(status="publish-error", error=sanitize(e))
                for o in outcomes:
                    if o.status == "ok":
                        self._set(o.item, ["queued"])
                raise
        for o in outcomes:
            if o.status == "ok":
                self._apply_success(o)

        summary = {
            "processed": len(outcomes),
            "added": len(recipes),
            "published": published,
            "stopped": stopped or None,
            "statuses": {s: sum(1 for o in outcomes if o.status == s) for s in {o.status for o in outcomes}},
        }
        log(status="run-summary", **summary)
        return summary

    # ------------------------------------------------------------------ issue outcomes

    def _apply_immediate(self, o: Outcome) -> None:
        item = o.item
        if o.status == "duplicate":
            self._set(item, ["duplicate"], close="not_planned",
                      comment=f"Duplicate: {item.canonical} is already in the library.")
        elif o.status == "not-recipe":
            self._set(item, ["not-recipe"], close="not_planned",
                      comment=f"Not a recipe (no ingredients and no method found). {item.canonical}")
        elif o.status == "quota":
            self._set(item, ["queued"])
        elif o.status == "needs-local":
            if self.local:
                self._set(item, ["needs-local"])
            else:
                self._set(item, ["needs-local"], comment=(
                    "All fetchers were blocked (login wall / rate limit). Labelled `needs-local`; "
                    "the self-hosted runner will pick it up if enabled, or add `retry` later.\n\n"
                    f"`{o.error[:200]}`"))
        elif o.status == "failed":
            self._set(item, ["failed"], comment=(
                f"Failed after retry. Add the `retry` label to try again.\n\n`{o.error[:300]}`"))

    def _site_url(self, url: str) -> str:
        repo = os.environ.get("GITHUB_REPOSITORY") or (getattr(self.gh, "repo", "") or "")
        if "/" not in repo:
            return ""
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/#/{urls.shortcode(url)}"

    def _apply_success(self, o: Outcome) -> None:
        r = o.recipe
        assert r is not None
        lines = [
            f"### {r.name}",
            f"{r.cuisine} \u00b7 {r.type} \u00b7 {r.time} \u00b7 source: {o.source}",
            "",
            f"Original: {r.url}",
        ]
        site = self._site_url(r.url)
        if site:
            lines.append(f"Library: {site}")
        if r.needs_review:
            lines += ["", f"**Needs review:** {o.review_reason or 'see TBD items'}"]
        lines += ["", "**Ingredients**"] + [f"- {i}" for i in r.ingredients]
        lines += ["", "**Steps**"] + [f"{n}. {s}" for n, s in enumerate(r.steps, 1)]
        body = "\n".join(lines)
        if r.needs_review:
            self._set(o.item, ["done", "needs-review"], comment=body)
        else:
            self._set(o.item, ["done"], close="completed", comment=body)
