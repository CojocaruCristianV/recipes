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
        "fallback_models": [],
        "temperature": 0.2,
        "inline_max_mb": 20,
        "file_poll_timeout_s": 300,
    },
    "fetch": {
        "order": ["apify", "ytdlp"],
        "local_order": ["ytdlp"],
        "apify_actor": "apify/instagram-scraper",
        "apify_timeout_s": 180,
        "max_media_mb": 200,
        "max_images": 10,
    },
    "run": {
        "max_items_per_run": 15,
        "run_budget_s": 660,
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
