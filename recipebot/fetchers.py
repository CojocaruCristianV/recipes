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
import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

import requests

from .urls import UA


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


def download_cdn(urls: list[str], dest: Path, default_mime: str, max_mb: int) -> list[MediaFile]:
    out: list[MediaFile] = []
    limit = max_mb * 1024 * 1024
    for i, url in enumerate(urls):
        ext = ".mp4" if default_mime.startswith("video/") else ".jpg"
        path = dest / f"media_{i}{ext}"
        try:
            with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=60) as r:
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


def fetch_apify(url: str, cfg: dict) -> Post:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise Unavailable("APIFY_TOKEN not set")
    from apify_client import ApifyClient
    from apify_client.errors import ApifyApiError

    fcfg = cfg["fetch"]
    client = ApifyClient(token)
    run_input = {
        "directUrls": [url],
        "resultsType": "posts",
        "resultsLimit": 1,
        "addParentData": False,
    }
    try:
        run = client.actor(fcfg["apify_actor"]).call(
            run_input=run_input,
            run_timeout=timedelta(seconds=int(fcfg["apify_timeout_s"])),
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
    items = client.dataset(dataset_id).list_items(limit=5, clean=True).items or []
    if not items:
        raise Blocked("Apify returned no items (private, login-only or blocked)")
    item = dict(items[0])
    err = item.get("error") or item.get("errorDescription")
    if err:
        text = f"{err} {item.get('errorDescription', '')}"
        if _looks_blocked(text):
            raise Blocked(f"Apify: {text[:160]}")
        raise FetchError(f"Apify: {text[:160]}")

    caption = item.get("caption") or ""
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

    return Post(url, caption, kind, "apify", videos, images, _dl if (videos or images) else None)


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

    return Post(url, caption, kind, "ytdlp", _downloader=_dl if has_video else None)


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
