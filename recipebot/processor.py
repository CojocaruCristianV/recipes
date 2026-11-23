"""Queue processing: issue -> fetch -> Gemini -> Recipe -> publish -> issue updates.

Three kinds of issues:
  add     a shared Instagram link (iOS Shortcut): becomes a new recipe
  edit    a change made on the website: checked and applied as is, no Gemini call
  reread  "Re-read from video" on the website: ingredients and steps are read again
"""
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
from typing import Callable
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from . import clean, edits, fetchers, store, urls
from .gemini import Gemini, GeminiBusy, GeminiError, ModelUnavailable, QuotaExceeded, media_kind
from .models import Extraction, Recipe

STATUS_LABELS = [
    "queued", "retry", "processing", "done", "failed", "duplicate",
    "not-recipe", "needs-review", "needs-local",
]

# Human wording for each processing step, used in logs and issue comments.
STEP_TEXT = {
    "fetch": "fetching the post",
    "caption": "reading the caption",
    "download": "downloading the video",
    "video": "reading the video",
    "images": "reading the images",
    "build": "building the recipe",
    "edit": "applying the edit",
}

RETRY_HOWTO = (
    "**To try again:** add the `retry` label to this issue. In the GitHub app: open the issue, "
    "tap **Labels** (or **...** > **Edit labels**), tick `retry`. The bot starts within a minute."
)


class NotRecipe(Exception):
    pass


class NothingToReread(Exception):
    """The post has no video or images, so a re-read cannot add anything."""


_SECRET_RE = re.compile(
    r"(apify_api_[A-Za-z0-9]+|AIza[0-9A-Za-z_\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|(?:token|key|sig|signature)=[^&\s]+)"
)


def sanitize(msg, limit: int = 300) -> str:
    return _SECRET_RE.sub("[redacted]", str(msg or "")).strip()[:limit]


def log(**fields) -> None:
    fields = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **fields}
    print(json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


@dataclass
class Item:
    url: str  # raw URL as shared (or as named by an edit / re-read request)
    issue: dict | None = None
    canonical: str = ""
    kind: str = "add"  # "add" | "edit" | "reread"
    data: dict | None = None  # parsed request block of an edit / re-read issue

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
    step: str = ""
    retries: int = 0  # Gemini busy retries used for this item
    model: str = ""  # Gemini model(s) that answered for this item, e.g. "gemini-3.7-flash"
    media_mb: float | None = None
    kind: str = "add"
    # Edit / re-read: the change to publish, the record before and after, notes for the comment.
    update: store.Update | None = None
    before: dict | None = None
    after: dict | None = None
    warnings: list[str] = field(default_factory=list)
    commit: str = ""


@dataclass
class _Work:
    """Per-item state kept across attempts so a retry never repeats a step that worked."""
    tmp: Path
    post: fetchers.Post | None = None
    cap: Extraction | None = None
    cap_done: bool = False
    media: list[fetchers.MediaFile] | None = None
    media_mb: float | None = None
    step: str = "fetch"


@dataclass
class Processor:
    cfg: dict
    dry_run: bool = False
    local: bool = False
    force: bool = False
    gh: object | None = None
    _gemini: Gemini | None = field(default=None, init=False)
    _deadline: float | None = field(default=None, init=False)

    # ------------------------------------------------------------------ setup

    @property
    def gemini(self) -> Gemini:
        if self._gemini is None:
            self._gemini = Gemini(self.cfg, deadline=self._deadline)
        return self._gemini

    def today(self) -> str:
        return datetime.now(ZoneInfo(self.cfg["run"]["timezone"])).strftime("%Y-%m-%d")

    @staticmethod
    def item_from_issue(issue: dict) -> Item:
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        names = {lb["name"] for lb in issue.get("labels", [])}
        data = edits.payload(issue.get("body"))
        # The label decides; the request block is the fallback if the labels were dropped.
        kind = next((k for k in edits.KINDS if k in names), "") or (data or {}).get("recipebot") or "add"
        url = (data or {}).get("url") if kind != "add" else None
        return Item(url=str(url or urls.extract_url(text) or ""), issue=issue, kind=kind, data=data)

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

    def _repo(self) -> str:
        return os.environ.get("GITHUB_REPOSITORY") or (getattr(self.gh, "repo", "") or "")

    # ------------------------------------------------------------------ extraction

    def _media(self, work: _Work) -> list[fetchers.MediaFile]:
        """Download the post media once per item; later attempts reuse the files."""
        if work.media is None:
            work.step = "download"
            assert work.post is not None
            work.media = work.post.download(work.tmp)
            work.media_mb = round(sum(m.size for m in work.media) / 1048576, 1) if work.media else 0.0
        return work.media

    def _extract(self, work: _Work, cuisines: list[str], types_: list[str]) -> tuple[Extraction, str, bool, bool]:
        """Return (extraction, source, ingredients_from_caption, steps_from_caption)."""
        post = work.post
        assert post is not None
        caption = (post.caption or "").strip()
        if caption and not work.cap_done:
            work.step = "caption"
            work.cap = self.gemini.from_caption(caption, cuisines, types_)
            work.cap_done = True
        cap = work.cap
        cap_ing = bool(cap and cap.caption_has_ingredients and cap.ingredients)
        cap_steps = bool(cap and cap.caption_has_steps and cap.steps)
        if cap_ing and cap_steps:
            assert cap is not None
            return cap, "caption", True, True

        missing = [p for p, have in (("ingredients", cap_ing), ("steps", cap_steps)) if not have]
        if post.has_media:
            media = self._media(work)
            if media:
                kind = media_kind(media)
                work.step = kind
                try:
                    med = self.gemini.from_media(caption, media, missing, cuisines, types_)
                except GeminiBusy:
                    # Keep what the caption gave instead of losing the recipe; the gap is
                    # marked TBD and flagged for review (fix later by editing or re-reading).
                    if cap and cap.is_recipe and (cap.ingredients or cap.steps):
                        cap.needs_review = True
                        cap.review_reason = (
                            f"The {kind} could not be read (Gemini was busy), so the "
                            f"{' and '.join(missing)} are missing"
                        )
                        return cap, f"caption (partial, {kind} not read)", cap_ing, cap_steps
                    raise
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

    # ------------------------------------------------------------------ per item

    def process_one(self, item: Item, known: set[str], cuisines: list[str], types_: list[str],
                    state: dict[str, dict] | None = None) -> Outcome:
        """state: url -> current record, kept up to date within the run (edits and re-reads)."""
        state = state if state is not None else {}
        if item.kind == "edit":
            return self.process_edit(item, state, cuisines, types_)
        if item.kind == "reread":
            return self.process_reread(item, state, cuisines, types_)
        if not item.url:
            return Outcome(item, "failed", error="No instagram.com URL found in the issue", step="fetch")
        try:
            item.canonical = urls.resolve(item.url)
        except ValueError as e:
            return Outcome(item, "failed", error=sanitize(e), step="fetch")

        reprocess = self.force or "retry" in item.labels
        if item.canonical in known and not reprocess:
            return Outcome(item, "duplicate")
        known.add(item.canonical)

        self._set(item, ["processing"])
        if self._gemini is not None:
            self._gemini.start_item()

        def run(work: _Work) -> Outcome:
            ex, source, ci, cs = self._extract(work, cuisines, types_)
            work.step = "build"
            recipe = self._build(ex, item.canonical, ci, cs, cuisines, types_)
            return self._out(item, work, "ok", recipe=recipe, source=source, review_reason=ex.review_reason)

        with tempfile.TemporaryDirectory(prefix="rb_media_") as tmp:
            return self._attempts(item, _Work(tmp=Path(tmp)), run)

    def process_edit(self, item: Item, state: dict[str, dict], cuisines: list[str], types_: list[str]) -> Outcome:
        """Validate a website edit against the current recipe. Never calls Gemini."""
        def out(status: str, **kw) -> Outcome:
            return Outcome(item, status, kind="edit", step="edit", **kw)

        data = item.data
        if not data or data.get("recipebot") != "edit":
            return out("invalid", error="The edit details are missing from this issue. Make the edit again on the website.")
        cu, ty = list(cuisines), list(types_)  # snapshot, so a re-apply at publish time gives the same result
        try:
            item.canonical = edits.target_url(data, item.url)
            current = state.get(item.canonical)
            if current is None:
                raise store.Missing("This recipe is no longer in the library.")
            edits.check_base(current, data)
            after, warnings = edits.build_edit(current, data, cu, ty)
            after = Recipe.model_validate(after).to_json()
        except edits.Conflict as e:
            return out("conflict", error=str(e))
        except (edits.EditError, store.Missing) as e:
            return out("invalid", error=str(e))
        except ValidationError as e:
            return out("invalid", error=sanitize(f"The edited recipe is not valid: {e}"))

        def apply(cur: dict) -> dict:
            # Runs again on the freshly pulled recipes.json before every push attempt.
            edits.check_base(cur, data)
            return edits.build_edit(cur, data, cu, ty)[0]

        state[item.canonical] = after
        return out("ok", before=current, after=after, warnings=warnings,
                   update=store.Update(item.canonical, apply))

    def process_reread(self, item: Item, state: dict[str, dict], cuisines: list[str], types_: list[str]) -> Outcome:
        """Read ingredients and steps again from caption + media; everything else stays."""
        try:
            item.canonical = edits.target_url(item.data, item.url)
        except edits.EditError as e:
            return Outcome(item, "invalid", kind="reread", step="fetch", error=str(e))
        if item.canonical not in state:
            return Outcome(item, "invalid", kind="reread", step="fetch",
                           error="This recipe is no longer in the library.")
        self._set(item, ["processing"])
        if self._gemini is not None:
            self._gemini.start_item()

        def run(work: _Work) -> Outcome:
            post = work.post
            assert post is not None
            if not post.has_media:
                raise NothingToReread("This post has no video or images, so there was nothing to re-read.")
            media = self._media(work)
            if not media:
                raise NothingToReread("The post's video or images could not be found, so there was nothing to re-read.")
            kind = media_kind(media)
            work.step = kind
            ex = self.gemini.reread(post.caption or "", media, cuisines, types_)
            work.step = "build"
            ingredients = clean.clean_lines(ex.ingredients, numbering=False)
            steps = clean.clean_lines(ex.steps, numbering=True)
            if not ingredients and not steps:
                # GeminiError is retried once by _attempts; the recipe is never emptied.
                raise GeminiError(f"The {kind} was read, but no ingredients or steps came back")
            warnings = []
            if not ingredients:
                warnings.append("No ingredients came back, so the current ingredients were kept.")
            if not steps:
                warnings.append("No steps came back, so the current steps were kept.")
            flagged = bool(ex.needs_review)

            def apply(cur: dict) -> dict:
                rec = dict(cur)
                if ingredients:
                    rec["ingredients"] = list(ingredients)
                if steps:
                    rec["steps"] = list(steps)
                rec["needs_review"] = flagged or clean.has_tbd(rec["ingredients"]) or clean.has_tbd(rec["steps"])
                return rec

            before = state[item.canonical]
            after = Recipe.model_validate(apply(before)).to_json()
            state[item.canonical] = after
            return self._out(item, work, "ok", kind="reread", source=kind, before=before, after=after,
                             warnings=warnings, review_reason=ex.review_reason if after["needs_review"] else "",
                             update=store.Update(item.canonical, apply))

        with tempfile.TemporaryDirectory(prefix="rb_media_") as tmp:
            return self._attempts(item, _Work(tmp=Path(tmp)), run, kind="reread")

    def _out(self, item: Item, work: _Work, status: str, **kw) -> Outcome:
        g = self._gemini
        retries = g.busy_retries if g else 0
        model = " + ".join(g.used) if g else ""
        return Outcome(item, status, step=work.step, retries=retries, model=model, media_mb=work.media_mb,
                       fetcher=work.post.fetcher if work.post else "", **kw)

    def _attempts(self, item: Item, work: _Work, run: Callable[[_Work], Outcome], kind: str = "add") -> Outcome:
        """Fetch the post (once) and call run(work), with one retry for ordinary errors."""
        order = self.cfg["fetch"]["local_order" if self.local else "order"]
        last_err = ""

        def out(status: str, **kw) -> Outcome:
            return self._out(item, work, status, kind=kind, **kw)

        for attempt in range(2):  # one retry for ordinary errors; busy errors retry inside Gemini
            try:
                if work.post is None:
                    work.step = "fetch"
                    work.post = fetchers.fetch(item.canonical, self.cfg, order)
                return run(work)
            except QuotaExceeded as e:
                return out("quota", error=sanitize(e))
            except ModelUnavailable as e:
                return out("model-unavailable", error=sanitize(e))
            except GeminiBusy as e:
                return out("busy", error=sanitize(e.detail))
            except fetchers.AllBlocked as e:
                return out("needs-local", error=sanitize(e))
            except NotRecipe as e:
                return out("not-recipe", error=sanitize(e))
            except NothingToReread as e:
                return out("nothing", error=sanitize(e))
            except (fetchers.FetchError, GeminiError, ValidationError, OSError, RuntimeError, ValueError) as e:
                last_err = sanitize(f"{type(e).__name__}: {e}")
                if isinstance(e, GeminiError) and "not set" in str(e):
                    raise
            except Exception as e:  # noqa: BLE001 - last-resort guard per item
                last_err = sanitize(f"{type(e).__name__}: {e}")
            if attempt == 0:
                time.sleep(3)
        return out("failed", error=last_err)

    # ------------------------------------------------------------------ run

    def run(self, items: list[Item]) -> dict:
        start = time.monotonic()
        # Busy waits never run past this point, so the job always has time to publish.
        self._deadline = start + int(self.cfg["run"].get("hard_deadline_s") or 780)
        existing = store.load()
        known = store.urls(existing)
        state = {r["url"]: r for r in existing if r.get("url")}
        cuisines = store.distinct(existing, "cuisine")
        types_ = store.distinct(existing, "type")
        budget = int(self.cfg["run"]["run_budget_s"])
        limit = int(self.cfg["run"]["max_items_per_run"])

        outcomes: list[Outcome] = []
        stopped = ""
        for idx, item in enumerate(items):
            if idx >= limit or time.monotonic() - start > budget:
                stopped = stopped or "budget"
                break
            if stopped and item.kind != "edit":
                continue  # Gemini stopped this run; edits need no Gemini, the rest stays queued
            t0 = time.monotonic()
            out = self.process_one(item, known, cuisines, types_, state)
            outcomes.append(out)
            log(issue=item.number, kind=item.kind if item.kind != "add" else None,
                url=item.canonical or item.url, status=out.status,
                source=out.source, fetcher=out.fetcher, model=out.model or None, step=out.step or None,
                busy_retries=out.retries or None, media_mb=out.media_mb,
                ms=int((time.monotonic() - t0) * 1000), error=out.error or None)
            if out.recipe:
                for value, pool in ((out.recipe.cuisine, cuisines), (out.recipe.type, types_)):
                    if value not in pool:
                        pool.append(value)
            self._apply_immediate(out)
            if out.status in ("quota", "model-unavailable", "busy") and not stopped:
                # Busy: Gemini is overloaded right now; the rest would most likely fail too.
                stopped = out.status

        recipes = [o.recipe for o in outcomes if o.status == "ok" and o.recipe]
        upd = [o for o in outcomes if o.status == "ok" and o.update]
        published = False
        if self.dry_run:
            for o in upd:
                log(issue=o.item.number, kind=o.kind, status="dry-run-changes",
                    changes=edits.changes(o.before or {}, o.after or {}))
            print(json.dumps([r.to_json() for r in recipes] + [o.after for o in upd], ensure_ascii=False, indent=2))
        elif recipes or upd:
            try:
                res = store.publish(recipes, [o.update for o in upd], int(self.cfg["run"]["max_push_attempts"]))
            except store.PublishError as e:
                log(status="publish-error", error=sanitize(e))
                for o in outcomes:
                    if o.status == "ok":
                        self._set(o.item, ["queued"])
                raise
            published = res.pushed
            for o, applied, err in zip(upd, res.applied, res.errors):
                if err is not None:
                    # Refused against the freshly pulled file (e.g. changed by a push meanwhile).
                    o.status = "conflict" if isinstance(err, edits.Conflict) else "invalid"
                    o.error = sanitize(err)
                    log(issue=o.item.number, kind=o.kind, status=o.status, error=o.error)
                    self._apply_immediate(o)
                else:
                    o.before, o.after = applied
                    o.commit = res.commit if res.pushed and applied[0] != applied[1] else ""
        for o in outcomes:
            if o.status == "ok":
                self._apply_success(o)

        g = self._gemini
        summary = {
            "model": g.model if g else self.cfg["gemini"]["model"],
            "models_used": (g.run_used or None) if g else None,
            "processed": len(outcomes),
            "added": len(recipes),
            "updated": sum(1 for o in upd if o.status == "ok" and o.before != o.after),
            "published": published,
            "stopped": stopped or None,
            "statuses": {s: sum(1 for o in outcomes if o.status == s) for s in {o.status for o in outcomes}},
        }
        log(status="run-summary", **summary)
        return summary

    # ------------------------------------------------------------------ issue outcomes

    def _apply_immediate(self, o: Outcome) -> None:
        item = o.item
        step = STEP_TEXT.get(o.step, o.step or "processing")
        what = {"edit": "this edit", "reread": "this re-read"}.get(o.kind, "this recipe")
        if o.status == "duplicate":
            self._set(item, ["duplicate"], close="not_planned",
                      comment=f"Duplicate: {item.canonical} is already in the library.")
        elif o.status == "not-recipe":
            self._set(item, ["not-recipe"], close="not_planned",
                      comment=f"Not a recipe (no ingredients and no method found). {item.canonical}")
        elif o.status == "conflict":
            self._set(item, ["failed"], close="not_planned",
                      comment=f"**Edit not applied.** {o.error}")
        elif o.status == "invalid":
            self._set(item, ["failed"], close="not_planned",
                      comment=f"**Could not apply {what}.** {o.error}")
        elif o.status == "nothing":
            self._set(item, ["failed"], close="not_planned",
                      comment=f"**Nothing to re-read.** {o.error} The recipe was not changed.")
        elif o.status in ("quota", "model-unavailable"):
            self._set(item, ["queued"])
        elif o.status == "busy":
            tries = f"tried {o.retries} more time{'s' if o.retries != 1 else ''}" if o.retries else "no time left to retry"
            if self._gemini is not None and len(self._gemini.models) > 1:
                tries += ", across all fallback models"
            self._set(item, ["queued"], comment=(
                f"**Gemini was busy** while {step} ({tries}). Nothing is lost: {what} stays "
                "queued and is tried again automatically with the next recipe you share, or in "
                "the daily morning run.\n\n"
                "To try sooner, add the `retry` label (GitHub app: open the issue, tap **Labels**, "
                f"tick `retry`).\n\n`{o.error[:200]}`"))
        elif o.status == "needs-local":
            if self.local:
                self._set(item, ["needs-local"])
            else:
                self._set(item, ["needs-local"], comment=(
                    "All fetchers were blocked (login wall / rate limit). Labelled `needs-local`; "
                    "the self-hosted runner will pick it up if enabled, or add `retry` later.\n\n"
                    f"`{o.error[:200]}`"))
        elif o.status == "failed":
            head = "Could not add this recipe" if o.kind == "add" else f"Could not finish {what}"
            self._set(item, ["failed"], comment=(
                f"**{head}.** It failed while {step}, also on the second try.\n\n"
                f"`{o.error[:300]}`\n\n{RETRY_HOWTO}"))

    def _site_url(self, url: str) -> str:
        repo = self._repo()
        if "/" not in repo:
            return ""
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/#/r/{urls.shortcode(url)}"

    def _apply_success(self, o: Outcome) -> None:
        if o.kind in ("edit", "reread"):
            self._apply_update_success(o)
            return
        r = o.recipe
        assert r is not None
        lines = [
            f"### {r.name}",
            f"{r.cuisine} \u00b7 {r.type} \u00b7 {r.time} \u00b7 source: {o.source}"
            + (f" \u00b7 model: {o.model}" if o.model else ""),
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

    def _apply_update_success(self, o: Outcome) -> None:
        r = o.after or {}
        title = "Edited" if o.kind == "edit" else "Re-read"
        lines = [f"### {title}: {r.get('name', '')}"]
        before = o.before or {}
        if o.kind == "reread":
            # A re-read usually rewrites most lines, so the new lists say more than a diff.
            nb = (len(before.get("ingredients") or []), len(before.get("steps") or []))
            na = (len(r.get("ingredients") or []), len(r.get("steps") or []))
            lines.append(f"Ingredients and steps were read again from the {o.source} together with the "
                         "caption" + (f" (model: {o.model})" if o.model else "") +
                         ". Name, cuisine, type, time and date were kept.")
            if before == r:
                lines += ["", "No changes: the re-read gave the same ingredients and steps."]
            else:
                lines += ["", f"Now {na[0]} ingredients and {na[1]} steps (before: {nb[0]} and {nb[1]})."]
            lines += ["", "**Ingredients**"] + [f"- {i}" for i in r.get("ingredients") or []]
            lines += ["", "**Steps**"] + [f"{n}. {s}" for n, s in enumerate(r.get("steps") or [], 1)]
        else:
            changes = edits.changes(before, r)
            if changes:
                lines += ["", "**Changes**"] + changes
            else:
                lines += ["", "No changes: the recipe already looked like this."]
        for w in o.warnings:
            lines += ["", f"**Note:** {w}"]
        if r.get("needs_review"):
            lines += ["", f"**Needs review:** {o.review_reason or 'see the TBD items'}"]
        site = self._site_url(r.get("url", ""))
        if site:
            lines += ["", f"Library: {site} (live in about a minute)"]
        repo = self._repo()
        if o.commit and "/" in repo:
            lines += ["", f"Commit: https://github.com/{repo}/commit/{o.commit}",
                      "To undo: open `recipes.json` on GitHub, tap **History**; the previous version "
                      "is the commit before this one."]
        labels = ["done"] + (["needs-review"] if r.get("needs_review") else [])
        self._set(o.item, labels, close="completed", comment="\n".join(lines))
