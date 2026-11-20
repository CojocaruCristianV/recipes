"""Command line entry point.

  python -m recipebot --url <link> [--dry-run]
  python -m recipebot --issue N [--dry-run]
  python -m recipebot --all-queued [--dry-run] [--local]
"""
from __future__ import annotations

import argparse
import os
import sys

from . import config as config_mod
from .config import ROOT
from .gemini import GeminiError
from .processor import STATUS_LABELS, Item, Processor, log, sanitize
from .store import PublishError

QUEUE_LABELS = ["queued", "retry", "processing"]
LOCAL_LABELS = ["needs-local"]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recipebot", description="Instagram post -> recipes.json")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="Process a single Instagram link (no issue involved)")
    src.add_argument("--issue", type=int, help="Process one issue by number")
    src.add_argument("--all-queued", action="store_true",
                     help="Process all open owner issues labelled queued/retry (or needs-local with --local)")
    p.add_argument("--dry-run", action="store_true", help="Print JSON; no commit, no issue changes")
    p.add_argument("--local", action="store_true", help="Self-hosted mode: needs-local issues, local fetcher order")
    p.add_argument("--force", action="store_true", help="Reprocess even if the URL is already in recipes.json")
    p.add_argument("--config", help="Path to config.toml (default: repo root)")
    return p


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_parser().parse_args(argv)
    cfg = config_mod.load(args.config)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    gh = None
    items: list[Item]
    try:
        if args.url:
            items = [Item(url=args.url)]
            if not args.dry_run:
                log(status="info", msg="--url without --dry-run commits and pushes recipes.json")
        else:
            from .gh import GitHub

            gh = GitHub()
            if args.issue:
                issue = gh.issue(args.issue)
                if not gh.is_owner_issue(issue):
                    log(issue=args.issue, status="ignored", error="Issue not authored by the repo owner")
                    return 0
                if issue.get("state") != "open" and not args.force:
                    log(issue=args.issue, status="ignored", error="Issue is closed (use --force)")
                    return 0
                items = [Processor.item_from_issue(issue)]
            else:
                if args.local:
                    issues = gh.open_issues(LOCAL_LABELS)
                else:
                    # Also pick up owner issues that carry no status label at all.
                    issues = gh.open_issues(QUEUE_LABELS, status_labels=STATUS_LABELS)
                items = [Processor.item_from_issue(i) for i in issues]
            if not args.dry_run and not gh.has_token:
                log(status="error", error="No GitHub token (set GITHUB_TOKEN or run 'gh auth login')")
                return 2

        if not items:
            log(status="run-summary", processed=0, msg="queue empty")
            return 0
        proc = Processor(cfg, dry_run=args.dry_run, local=args.local, force=args.force, gh=gh)
        summary = proc.run(items)
    except GeminiError as e:
        log(status="error", error=sanitize(e))
        return 2
    except PublishError as e:
        log(status="error", error=sanitize(e))
        return 3
    except Exception as e:  # noqa: BLE001
        log(status="error", error=sanitize(f"{type(e).__name__}: {e}"))
        return 1

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"published={'true' if summary['published'] else 'false'}\n")
            fh.write(f"added={summary['added']}\n")
    return 0
