"""Load config.toml and merge it over built-in defaults."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "gemini": {
        "model": "gemini-3.8-flash",
        "fallback_models": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
        "temperature": 0.2,
        "inline_max_mb": 20,
        "file_poll_timeout_s": 300,
        "upload_video": True,
        "busy_waits_s": [30, 60, 120],
        "request_timeout_s": 240,
        "light_retry": True,
        "thinking_level": "LOW",
        "min_interval_s": 4.5,
        "rpm_waits_s": [20, 40],
    },
    "fetch": {
        "order": ["apify", "ytdlp"],
        "local_order": ["ytdlp"],
        "apify_actor": "apify/instagram-scraper",
        "apify_timeout_s": 180,
        "max_media_mb": 200,
        "max_images": 10,
    },
    "web": {
        "enabled": True,
        "follow_bio_links": True,
        "apify_fallback": True,
        "apify_actor": "apify/cheerio-scraper",
        "apify_timeout_s": 120,
        "max_apify_runs": 2,
        "max_fetches": 15,
        "max_pages": 6,
        "max_bio_links": 3,
        "max_seconds": 120,
        "timeout_s": 12,
        "max_page_mb": 3,
        "max_text_chars": 12000,
        "min_name_score": 0.7,
        "link_pages": [],
        "skip_hosts": [],
    },
    "run": {
        "max_items_per_run": 15,
        "run_budget_s": 660,
        "hard_deadline_s": 720,
        "timezone": "Europe/Bucharest",
        "max_push_attempts": 5,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: str | Path | None = None) -> dict:
    """Defaults < config.toml < GEMINI_MODEL env var (repo variable / .env)."""
    path = Path(path) if path else ROOT / "config.toml"
    if path.exists():
        with path.open("rb") as fh:
            cfg = _merge(DEFAULTS, tomllib.load(fh))
    else:
        cfg = copy.deepcopy(DEFAULTS)
    override = os.environ.get("GEMINI_MODEL", "").strip()
    if override:
        cfg["gemini"]["model"] = override
    return cfg
