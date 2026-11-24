"""Fetch Instagram post data through a configurable fetcher chain.

Each fetcher returns a Post (caption + media references). Media bytes are only
downloaded later, on demand, via Post.download(dir).

Error classes:
  Blocked      - login wall / rate limit / IP block (counts toward "needs-local")
  Unavailable  - fetcher not usable right now (no token, out of credit); neutral
  FetchError   - anything else (post deleted, parse error, ...)
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from .urls import UA

# Instagram usernames: letters, digits, "." and "_", at most 30 characters.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# Media is only downloaded from Instagram's own CDN hosts.
_CDN_HOSTS = ("cdninstagram.com", "fbcdn.net")
# Fields of the Apify post item this code relies on. A missing one means the actor's
# output changed; that is logged so it does not silently degrade results.
_EXPECTED_POST_FIELDS = ("caption", "type", "ownerUsername", "displayUrl", "latestComments")


def _log(**fields) -> None:
    print(json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


class FetchError(Exception):
    pass


class Blocked(FetchError):
    pass


class Unavailable(FetchError):
    pass


class AllBlocked(FetchError):
    """Every fetcher was blocked (or unavailable, with at least one block)."""


_BLOCK_RE = re.compile(
    r"login|log in|logged.in|sign in|rate.?limit|too many requests|\b429\b|\b401\b|\b403\b|"
    r"checkpoint|challenge|restricted|private|cookies|forbidden|not available in your",
    re.I,
)


def _looks_blocked(msg: str) -> bool:
    return bool(_BLOCK_RE.search(msg or ""))


@dataclass
class MediaFile:
    path: Path
    mime: str

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass
class Post:
    url: str
    caption: str
    kind: str  # "video" | "image" | "carousel" | "unknown"
    fetcher: str
    video_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    _downloader: Callable[[Path], list[MediaFile]] | None = field(default=None, repr=False)
    author: str = ""  # Instagram username, "" if unknown
    author_name: str = ""  # display name, "" if unknown
    # Comments written by the creator on this post (top-level and replies), newest first.
    creator_comments: list[str] = field(default_factory=list)
    # Loads more comments (a separate Apify run); only used when the caption points to them.
    _more_comments: Callable[[], list[str]] | None = field(default=None, repr=False)

    def more_comments(self) -> list[str]:
        """The creator's comments from a deeper comments fetch ([] if not available)."""
        if self._more_comments is None:
            return []
        try:
            extra = self._more_comments()
        except Exception as e:  # noqa: BLE001 - comments are a bonus, never fatal
            _log(status="comments-error", url=self.url, error=str(e)[:160])
            extra = []
        self._more_comments = None
        new = [c for c in extra if c not in self.creator_comments]
        self.creator_comments += new
        return new

    @property
    def has_media(self) -> bool:
        return bool(self.video_urls or self.image_urls or self._downloader)

    def download(self, dest: Path) -> list[MediaFile]:
        if self._downloader is None:
            return []
        return self._downloader(dest)


# --------------------------------------------------------------------------- CDN download


def _guess_mime(path: Path, header: str | None, default: str) -> str:
    if header:
        mime = header.split(";")[0].strip().lower()
        if mime.startswith(("video/", "image/")):
            return mime
    guess, _ = mimetypes.guess_type(path.name)
    return guess or default


def _is_cdn(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in _CDN_HOSTS)


def download_cdn(urls: list[str], dest: Path, default_mime: str, max_mb: int) -> list[MediaFile]:
    out: list[MediaFile] = []
    limit = max_mb * 1024 * 1024
    for i, url in enumerate(urls):
        if not _is_cdn(url):
            raise FetchError(f"Media URL is not on Instagram's CDN: {urlparse(url).hostname}")
        ext = ".mp4" if default_mime.startswith("video/") else ".jpg"
        path = dest / f"media_{i}{ext}"
        try:
            with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=60,
                              allow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    raise FetchError("Media download was redirected away from the CDN")
                if r.status_code in (401, 403, 429):
                    raise Blocked(f"CDN returned {r.status_code}")
                r.raise_for_status()
                size = 0
                with path.open("wb") as fh:
                    for chunk in r.iter_content(1024 * 256):
                        size += len(chunk)
                        if size > limit:
                            raise FetchError(f"Media larger than {max_mb} MB")
                        fh.write(chunk)
                out.append(MediaFile(path, _guess_mime(path, r.headers.get("Content-Type"), default_mime)))
        except requests.RequestException as e:
            raise FetchError(f"Media download failed: {type(e).__name__}") from None
    return out


# --------------------------------------------------------------------------- Apify


def _apify_run(run_input: dict, cfg: dict, limit: int = 5, timeout_s: int | None = None) -> list[dict]:
    """Run the configured Instagram actor once and return its dataset items."""
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise Unavailable("APIFY_TOKEN not set")
    from apify_client import ApifyClient
    from apify_client.errors import ApifyApiError

    fcfg = cfg["fetch"]
    client = ApifyClient(token)
    try:
        run = client.actor(fcfg["apify_actor"]).call(
            run_input=run_input,
            run_timeout=timedelta(seconds=int(timeout_s or fcfg["apify_timeout_s"])),
            logger=None,
        )
    except ApifyApiError as e:
        msg = str(e)
        if re.search(r"usage|credit|limit exceeded|payment|insufficient|unauthori|token", msg, re.I):
            raise Unavailable(f"Apify: {msg[:160]}") from None
        raise FetchError(f"Apify: {msg[:160]}") from None
    if run is None:
        raise FetchError("Apify: actor run did not return")
    status = str(getattr(run, "status", "") or "")
    if status and status.upper().endswith(("FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT")):
        raise FetchError(f"Apify run {status}")
    dataset_id = getattr(run, "default_dataset_id", None)
    if not dataset_id:
        raise FetchError("Apify: no dataset")
    return [dict(i) for i in (client.dataset(dataset_id).list_items(limit=limit, clean=True).items or [])]


def _owner_texts(comments: list, owner: str) -> list[str]:
    """Texts of the comments (and replies) written by the post owner."""
    out: list[str] = []
    if not owner:
        return out
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("ownerUsername") or "").lower() == owner.lower() and c.get("text"):
            out.append(str(c["text"]))
        for r in c.get("replies") or []:
            if isinstance(r, dict) and str(r.get("ownerUsername") or "").lower() == owner.lower() and r.get("text"):
                out.append(str(r["text"]))
    return list(dict.fromkeys(out))


def fetch_comments(url: str, owner: str, cfg: dict) -> list[str]:
    """The creator's comments on a post, from a separate comments run (up to 50 comments)."""
    items = _apify_run({"directUrls": [url], "resultsType": "comments", "resultsLimit": 50}, cfg, limit=60)
    return _owner_texts(items, owner)


_PROFILE_CACHE: dict[str, list[tuple[str, str]]] = {}


def profile_links(username: str, cfg: dict) -> list[tuple[str, str]]:
    """(title, url) of the links in a creator's bio. One Apify run per creator and run (cached)."""
    key = username.lower()
    if key in _PROFILE_CACHE:
        return _PROFILE_CACHE[key]
    links: list[tuple[str, str]] = []
    try:
        items = _apify_run({"directUrls": [f"https://www.instagram.com/{username}/"],
                            "resultsType": "details", "resultsLimit": 1}, cfg, limit=1)
        prof = items[0] if items else {}
        for ln in prof.get("externalUrls") or []:
            if isinstance(ln, dict) and str(ln.get("url") or "").startswith("http"):
                links.append((str(ln.get("title") or ""), str(ln["url"])))
        if str(prof.get("externalUrl") or "").startswith("http"):
            links.append(("", str(prof["externalUrl"])))
        for u in re.findall(r"https?://[^\s<>\"')]+", str(prof.get("biography") or "")):
            links.append(("", u))
    except FetchError as e:
        _log(status="profile-error", author=username, error=str(e)[:160])
    seen: set[str] = set()
    links = [(t, u) for t, u in links if not (u in seen or seen.add(u))]
    _PROFILE_CACHE[key] = links
    return links


def apify_usage() -> tuple[float, float] | None:
    """(used USD, limit USD) of the Apify account this billing month, or None if unknown."""
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        return None
    try:
        from apify_client import ApifyClient

        lim = ApifyClient(token).user().limits()
        used = float(getattr(getattr(lim, "current", None), "monthly_usage_usd", None))
        cap = float(getattr(getattr(lim, "limits", None), "max_monthly_usage_usd", None))
        return used, cap
    except Exception:  # noqa: BLE001 - informational only
        return None


def fetch_apify(url: str, cfg: dict) -> Post:
    fcfg = cfg["fetch"]
    run_input = {
        "directUrls": [url],
        "resultsType": "posts",
        "resultsLimit": 1,
        "addParentData": False,
    }
    items = _apify_run(run_input, cfg)
    if not items:
        raise Blocked("Apify returned no items (private, login-only or blocked)")
    item = dict(items[0])
    err = item.get("error") or item.get("errorDescription")
    if err:
        text = f"{err} {item.get('errorDescription', '')}"
        if _looks_blocked(text):
            raise Blocked(f"Apify: {text[:160]}")
        raise FetchError(f"Apify: {text[:160]}")
    missing = [k for k in _EXPECTED_POST_FIELDS if k not in item]
    if missing:
        _log(status="apify-format-warning", url=url, actor=fcfg["apify_actor"], missing=missing,
             msg="The Apify actor's output changed; check fetchers.py")

    caption = item.get("caption") or ""
    owner = str(item.get("ownerUsername") or "")
    owner = owner if USERNAME_RE.match(owner) else ""
    ptype = str(item.get("type") or item.get("productType") or "").lower()
    videos: list[str] = []
    images: list[str] = []
    if item.get("videoUrl"):
        videos.append(item["videoUrl"])
    for child in item.get("childPosts") or []:
        if child.get("videoUrl"):
            videos.append(child["videoUrl"])
        elif child.get("displayUrl"):
            images.append(child["displayUrl"])
    if not videos and not images:
        images = [u for u in (item.get("images") or []) if isinstance(u, str)]
        if not images and item.get("displayUrl"):
            images = [item["displayUrl"]]

    if videos and ("sidecar" not in ptype or not images):
        kind = "video"
    elif len(images) + len(videos) > 1:
        kind = "carousel"
    elif images:
        kind = "image"
    else:
        kind = "unknown"

    max_mb = int(fcfg["max_media_mb"])
    max_images = int(fcfg["max_images"])

    def _dl(dest: Path) -> list[MediaFile]:
        if videos:
            return download_cdn(videos[:1], dest, "video/mp4", max_mb)
        return download_cdn(images[:max_images], dest, "image/jpeg", max_mb)

    more = (lambda: fetch_comments(url, owner, cfg)) if owner else None
    return Post(url, caption, kind, "apify", videos, images, _dl if (videos or images) else None,
                author=owner, author_name=str(item.get("ownerFullName") or ""),
                creator_comments=_owner_texts(item.get("latestComments") or [], owner),
                _more_comments=more)


# --------------------------------------------------------------------------- yt-dlp


class _QuietLogger:
    def debug(self, msg):  # noqa: D401
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _ytdlp_opts(cookiefile: str | None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "socket_timeout": 30,
        "retries": 2,
        "noplaylist": False,
        "http_headers": {"User-Agent": UA},
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def _cookiefile() -> str | None:
    b64 = os.environ.get("IG_COOKIES_B64", "").strip()
    if not b64:
        return None
    fd, path = tempfile.mkstemp(prefix="igc_", suffix=".cookies.txt")
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64decode(b64))
    return path


def fetch_ytdlp(url: str, cfg: dict) -> Post:
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError:
        raise Unavailable("yt-dlp not installed") from None

    fcfg = cfg["fetch"]
    cookiefile = _cookiefile()
    try:
        try:
            with yt_dlp.YoutubeDL({**_ytdlp_opts(cookiefile), "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as e:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
            if _looks_blocked(msg):
                raise Blocked(f"yt-dlp: {msg[:160]}") from None
            raise FetchError(f"yt-dlp: {msg[:160]}") from None
    finally:
        if cookiefile:
            Path(cookiefile).unlink(missing_ok=True)
    if not info:
        raise FetchError("yt-dlp returned nothing")

    entries = [e for e in (info.get("entries") or []) if e] if info.get("_type") == "playlist" else [info]
    caption = info.get("description") or next((e.get("description") for e in entries if e.get("description")), "") or ""
    owner = next((str(v) for v in (info.get("channel"), info.get("uploader_id"), info.get("uploader"))
                  if v and USERNAME_RE.match(str(v)) and not str(v).isdigit()), "")
    has_video = any(e.get("formats") or e.get("url") for e in entries)
    kind = "video" if len(entries) == 1 and has_video else ("carousel" if len(entries) > 1 else "unknown")
    max_mb = int(fcfg["max_media_mb"])

    def _dl(dest: Path) -> list[MediaFile]:
        cf = _cookiefile()
        opts = {
            **_ytdlp_opts(cf),
            # Single-file MP4 first so ffmpeg is never required.
            "format": "b[ext=mp4][vcodec!=none]/b[vcodec!=none]/bv*[ext=mp4]/bv*",
            "outtmpl": str(dest / "media_%(autonumber)s.%(ext)s"),
            "max_filesize": max_mb * 1024 * 1024,
            "playlist_items": "1",
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except DownloadError as e:
            msg = str(e)
            if _looks_blocked(msg):
                raise Blocked(f"yt-dlp media: {msg[:160]}") from None
            raise FetchError(f"yt-dlp media: {msg[:160]}") from None
        finally:
            if cf:
                Path(cf).unlink(missing_ok=True)
        files = sorted(p for p in dest.iterdir() if p.is_file() and not p.name.endswith(".part"))
        return [MediaFile(p, _guess_mime(p, None, "video/mp4")) for p in files]

    return Post(url, caption, kind, "ytdlp", _downloader=_dl if has_video else None, author=owner,
                _more_comments=(lambda: fetch_comments(url, owner, cfg)) if owner else None)


# --------------------------------------------------------------------------- chain

FETCHERS: dict[str, Callable[[str, dict], Post]] = {
    "apify": fetch_apify,
    "ytdlp": fetch_ytdlp,
}


def fetch(url: str, cfg: dict, order: list[str]) -> Post:
    """Try fetchers in order. Raises AllBlocked or FetchError if none succeeds."""
    errors: list[FetchError] = []
    for name in order:
        fn = FETCHERS.get(name)
        if fn is None:
            errors.append(FetchError(f"Unknown fetcher {name!r}"))
            continue
        try:
            return fn(url, cfg)
        except FetchError as e:
            errors.append(e)
        except Exception as e:  # unexpected library errors
            msg = str(e)
            errors.append(Blocked(msg[:160]) if _looks_blocked(msg) else FetchError(f"{name}: {type(e).__name__}: {msg[:140]}"))
    summary = "; ".join(str(e) for e in errors) or "no fetchers configured"
    blocked = [e for e in errors if isinstance(e, Blocked)]
    neutral = [e for e in errors if isinstance(e, Unavailable)]
    if blocked and len(blocked) + len(neutral) == len(errors):
        raise AllBlocked(summary)
    raise FetchError(summary)
