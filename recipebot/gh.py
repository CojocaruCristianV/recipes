"""Minimal GitHub REST client for the issue queue."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import requests

from .config import ROOT

API = "https://api.github.com"


def _repo_from_git() -> str | None:
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN", "").strip() or os.environ.get("GH_TOKEN", "").strip()
    if tok:
        return tok
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


class GitHub:
    def __init__(self, repo: str | None = None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY") or _repo_from_git()
        if not self.repo:
            raise RuntimeError("Cannot determine repo: set GITHUB_REPOSITORY=owner/repo or add a git remote")
        self.owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or self.repo.split("/")[0]
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "recipebot",
        })
        tok = _token()
        if tok:
            self.s.headers["Authorization"] = f"Bearer {tok}"
        self.has_token = bool(tok)

    def _req(self, method: str, path: str, ok404: bool = False, **kw):
        r = self.s.request(method, f"{API}/repos/{self.repo}{path}", timeout=30, **kw)
        if ok404 and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:160]}")
        return r.json() if r.content else None

    def is_owner_issue(self, issue: dict) -> bool:
        return (
            "pull_request" not in issue
            and (issue.get("user") or {}).get("login", "").lower() == self.owner.lower()
        )

    def issue(self, number: int) -> dict:
        return self._req("GET", f"/issues/{number}")

    def open_issues(self, labels: list[str], status_labels: list[str] | None = None) -> list[dict]:
        """Open owner-authored issues having ANY of `labels`, oldest first.

        If status_labels is given, issues carrying none of them are included too
        (covers issues created without labels, e.g. when labels were dropped).
        """
        wanted = set(labels)
        status = set(status_labels or [])
        found: list[dict] = []
        page = 1
        while True:
            batch = self._req(
                "GET", "/issues",
                params={"state": "open", "sort": "created", "direction": "asc",
                        "per_page": 100, "page": page},
            ) or []
            for it in batch:
                if not self.is_owner_issue(it):
                    continue
                names = {lb["name"] for lb in it.get("labels", [])}
                if names & wanted or (status_labels is not None and not names & status):
                    found.append(it)
            if len(batch) < 100:
                break
            page += 1
        return sorted(found, key=lambda i: i["number"])

    def add_labels(self, number: int, labels: list[str]) -> None:
        if labels:
            self._req("POST", f"/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        self._req("DELETE", f"/issues/{number}/labels/{requests.utils.quote(label)}", ok404=True)

    def relabel(self, number: int, add: list[str], remove: list[str], current: list[str]) -> None:
        for lb in remove:
            if lb in current and lb not in add:
                self.remove_label(number, lb)
        self.add_labels(number, [lb for lb in add if lb not in current])

    def comment(self, number: int, body: str) -> None:
        self._req("POST", f"/issues/{number}/comments", json={"body": body[:60000]})

    def close(self, number: int, reason: str = "completed") -> None:
        self._req("PATCH", f"/issues/{number}", json={"state": "closed", "state_reason": reason})
