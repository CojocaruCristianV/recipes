"""Gemini calls via the google-genai SDK (JSON mode + response schema)."""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path
from string import Template

from pydantic import ValidationError

from .fetchers import MediaFile
from .models import Extraction

PROMPTS = Path(__file__).resolve().parent / "prompts"


class QuotaExceeded(Exception):
    """Gemini 429 / RESOURCE_EXHAUSTED: stop the run, keep items queued."""


class GeminiError(Exception):
    pass


class ModelUnavailable(GeminiError):
    """Every configured model is unknown / retired: stop the run, keep items queued."""


class GeminiBusy(GeminiError):
    """Gemini stayed overloaded / timed out through every retry."""

    def __init__(self, step: str, retries: int, detail: str):
        self.step = step
        self.retries = retries
        self.detail = detail
        super().__init__(f"Gemini busy while reading the {step} ({retries} retries): {detail}")


_MODEL_GONE_RE = re.compile(
    r"(model|models/)[^.]*\b(not found|not supported|unsupported|is not available|deprecated|retired)", re.I
)
# Temporary server-side trouble: overloaded, timed out, internal error.
_BUSY_CODES = {408, 500, 502, 503, 504}
_BUSY_STATUS = {"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"}
_BUSY_RE = re.compile(r"high demand|overloaded|try again later|temporarily|unavailable|deadline", re.I)
# Network level failures (httpx / requests / socket), matched by class name.
_NET_NAMES = ("Timeout", "ConnectError", "ConnectionError", "RemoteProtocolError", "ReadError", "WriteError")
# A busy retry is only started if this many seconds remain before the run deadline.
_MIN_CALL_S = 45


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _fmt_values(values: list[str]) -> str:
    return ", ".join(values) if values else "(none yet)"


def media_kind(media: list[MediaFile]) -> str:
    return "video" if any(m.mime.startswith("video/") for m in media) else "images"


def _log(**fields) -> None:
    print(json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


def is_busy(err: Exception) -> bool:
    """True for errors worth waiting for: 503 high demand, timeouts, 5xx, dropped connections."""
    from google.genai import errors

    if isinstance(err, errors.APIError):
        code = getattr(err, "code", None)
        if code in (400, 401, 403, 404, 429):
            return False
        status = str(getattr(err, "status", "") or "")
        msg = getattr(err, "message", None) or str(err)
        return code in _BUSY_CODES or status in _BUSY_STATUS or bool(_BUSY_RE.search(msg))
    names = [c.__name__ for c in type(err).__mro__]
    return any(key in name for name in names for key in _NET_NAMES)


def _short(err: Exception) -> str:
    code = getattr(err, "code", None)
    msg = getattr(err, "message", None) or str(err) or type(err).__name__
    return (f"{code}: {msg}" if code else f"{type(err).__name__}: {msg}")[:160]


class Gemini:
    def __init__(self, cfg: dict, deadline: float | None = None):
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise GeminiError("GEMINI_API_KEY not set")
        from google import genai
        from google.genai import types

        self.cfg = cfg["gemini"]
        # Per-request timeout; a request that hangs longer counts as "busy" and is retried.
        self._timeout_s = int(self.cfg.get("request_timeout_s") or 0)
        http = types.HttpOptions(timeout=self._timeout_s * 1000) if self._timeout_s > 0 else None
        self._genai = genai
        self.client = genai.Client(api_key=key, http_options=http)
        self.system = _prompt("system.txt")
        # Primary model first, then fallbacks; duplicates removed, order kept.
        chain = [self.cfg["model"], *(self.cfg.get("fallback_models") or [])]
        self.models = list(dict.fromkeys(m.strip() for m in chain if m and m.strip()))
        self._active = 0  # index of the model that last worked (sticky for the run)
        self.deadline = deadline  # time.monotonic() value; busy waits never run past it
        self._waits = [float(w) for w in (self.cfg.get("busy_waits_s") or [])]
        self._thinking = str(self.cfg.get("thinking_level") or "").strip().upper() or None
        self._light = bool(self.cfg.get("light_retry", True))
        self.busy_retries = 0  # reset by the caller per item; for logs and comments

    @property
    def model(self) -> str:
        return self.models[self._active]

    def list_models(self) -> list[str]:
        """Model IDs available to this key that support generateContent."""
        out = []
        for m in self.client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                out.append((m.name or "").removeprefix("models/"))
        return sorted(x for x in out if x)

    # ------------------------------------------------------------------ helpers

    def _remaining(self, step: str) -> float | None:
        """Seconds left before the run deadline; raises GeminiBusy if too little is left for a call."""
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left < _MIN_CALL_S:
            raise GeminiBusy(step, self.busy_retries, "no time left in this run")
        return left

    def _config(self, light: bool = False, step: str = ""):
        from google.genai import types

        kw: dict = dict(
            system_instruction=self.system,
            response_mime_type="application/json",
            response_schema=Extraction,
            temperature=float(self.cfg["temperature"]),
        )
        left = self._remaining(step)
        if left is not None and (self._timeout_s <= 0 or left - 10 < self._timeout_s):
            # Never let one request run past the run deadline.
            kw["http_options"] = types.HttpOptions(timeout=int((left - 10) * 1000))
        if self._thinking:
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=self._thinking)
        if light and self._light:
            kw["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
        return types.GenerateContentConfig(**kw)

    @staticmethod
    def _is_quota(err: Exception) -> bool:
        code = getattr(err, "code", None)
        text = str(err)
        return code == 429 or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower()

    @staticmethod
    def _is_model_gone(err: Exception) -> bool:
        code = getattr(err, "code", None)
        msg = getattr(err, "message", None) or str(err)
        return code == 404 or (code == 400 and bool(_MODEL_GONE_RE.search(msg)))

    def _drop_unsupported(self, err: Exception, light: bool) -> bool:
        """On a 400, switch off one optional setting and report True (call again).

        The exact wording of "setting not supported" errors is not documented, so any 400
        counts: first the thinking level goes, then low media resolution. Once both are off
        a 400 is a real error. This way an unsupported setting can never block extraction.
        """
        if getattr(err, "code", None) != 400:
            return False
        msg = (getattr(err, "message", None) or str(err))[:160]
        if self._thinking:
            _log(status="gemini-setting-dropped", setting="thinking_level", error=msg)
            self._thinking = None
            return True
        if light and self._light:
            _log(status="gemini-setting-dropped", setting="media_resolution", error=msg)
            self._light = False
            return True
        return False

    def _pause(self, attempt: int, step: str, err: Exception) -> None:
        """Sleep before busy retry number attempt+1, or raise GeminiBusy when out of retries / time."""
        detail = _short(err)
        if attempt >= len(self._waits):
            raise GeminiBusy(step, attempt, detail)
        wait = self._waits[attempt] + random.uniform(0, 5)
        if self.deadline is not None and time.monotonic() + wait + _MIN_CALL_S > self.deadline:
            raise GeminiBusy(step, attempt, f"{detail} (no time left in this run)")
        _log(status="gemini-busy", step=step, retry=attempt + 1, of=len(self._waits),
             wait_s=int(wait), model=self.model, error=detail)
        time.sleep(wait)
        self.busy_retries += 1

    def _call(self, contents: list, step: str, has_media: bool):
        """generate_content with busy retries; moves down the fallback chain if a model is gone."""
        from google.genai import errors

        attempt = 0
        while True:
            # Busy retries of media requests ask for low media resolution (far fewer tokens).
            light = has_media and attempt > 0
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=contents, config=self._config(light, step)
                )
            except errors.APIError as e:
                if self._is_quota(e):
                    raise QuotaExceeded(f"Gemini quota ({self.model}): {getattr(e, 'code', '')} {getattr(e, 'status', '')}".strip()) from None
                if self._is_model_gone(e):
                    if self._active + 1 < len(self.models):
                        self._active += 1
                        continue
                    raise ModelUnavailable(
                        f"Gemini model(s) unavailable: {', '.join(self.models)}. "
                        "Set the repo variable GEMINI_MODEL (see: python -m recipebot --list-models)"
                    ) from None
                if self._drop_unsupported(e, light):
                    continue
                if not is_busy(e):
                    raise GeminiError(f"Gemini API {getattr(e, 'code', '')}: {(getattr(e, 'message', None) or str(e))[:160]}") from None
                self._pause(attempt, step, e)
            except Exception as e:  # network errors and timeouts raised below the SDK
                if not is_busy(e):
                    raise
                self._pause(attempt, step, e)
            attempt += 1

    def _generate(self, contents: list, step: str, has_media: bool = False) -> Extraction:
        """Call the model; retry once on invalid output. Raises QuotaExceeded / ModelUnavailable / GeminiBusy."""
        last: Exception | None = None
        for _ in range(2):
            resp = self._call(contents, step, has_media)
            try:
                parsed = getattr(resp, "parsed", None)
                if isinstance(parsed, Extraction):
                    return parsed
                return Extraction.model_validate_json(resp.text or "")
            except (ValidationError, ValueError) as e:
                last = e
        raise GeminiError(f"Invalid model output after retry: {str(last)[:160]}")

    # ------------------------------------------------------------------ public

    def from_caption(self, caption: str, cuisines: list[str], types_: list[str]) -> Extraction:
        text = Template(_prompt("caption.txt")).safe_substitute(
            caption=caption.strip(),
            existing_cuisines=_fmt_values(cuisines),
            existing_types=_fmt_values(types_),
        )
        return self._generate([text], "caption")

    def from_media(
        self,
        caption: str,
        media: list[MediaFile],
        missing: list[str],
        cuisines: list[str],
        types_: list[str],
    ) -> Extraction:
        kind = media_kind(media)
        text = Template(_prompt("media.txt")).safe_substitute(
            caption=(caption or "").strip() or "(no caption)",
            media_kind=kind,
            missing=" and ".join(missing) or "nothing",
            existing_cuisines=_fmt_values(cuisines),
            existing_types=_fmt_values(types_),
        )
        return self._with_media(media, text, kind)

    def reread(self, caption: str, media: list[MediaFile], cuisines: list[str], types_: list[str]) -> Extraction:
        """Complete ingredients and steps from caption + media together (website "Re-read")."""
        kind = media_kind(media)
        text = Template(_prompt("reread.txt")).safe_substitute(
            caption=(caption or "").strip() or "(no caption)",
            media_kind=kind,
            existing_cuisines=_fmt_values(cuisines),
            existing_types=_fmt_values(types_),
        )
        return self._with_media(media, text, kind)

    def _with_media(self, media: list[MediaFile], text: str, kind: str) -> Extraction:
        """Send media (inline or via the File API) plus the prompt text; uploads are deleted afterwards."""
        from google.genai import types

        has_video = kind == "video"
        total = sum(m.size for m in media)
        inline_limit = int(self.cfg["inline_max_mb"]) * 1024 * 1024
        # Videos go through the File API by default: uploaded once, then every retry
        # only sends a small reference instead of the whole file again.
        inline = total < inline_limit and not (has_video and self.cfg.get("upload_video", True))
        uploaded: list = []
        try:
            parts: list = []
            if inline:
                for m in media:
                    parts.append(types.Part.from_bytes(data=m.path.read_bytes(), mime_type=m.mime))
            else:
                for m in media:
                    f = self._upload(m, kind)
                    uploaded.append(f)
                    parts.append(self._wait_active(f))
            return self._generate(parts + [text], kind, has_media=True)
        finally:
            for f in uploaded:
                try:
                    self.client.files.delete(name=f.name)
                except Exception:
                    pass

    def _upload(self, m: MediaFile, step: str):
        attempt = 0
        while True:
            self._remaining(step)
            try:
                return self.client.files.upload(file=str(m.path), config={"mime_type": m.mime})
            except Exception as e:
                if self._is_quota(e):
                    raise QuotaExceeded(f"Gemini quota (upload): {_short(e)}") from None
                if not is_busy(e):
                    raise GeminiError(f"Gemini upload failed: {_short(e)}") from None
                self._pause(attempt, step, e)
                attempt += 1

    def _wait_active(self, f):
        deadline = time.monotonic() + int(self.cfg["file_poll_timeout_s"])
        if self.deadline is not None:
            deadline = min(deadline, self.deadline - _MIN_CALL_S)
        while True:
            state = str(getattr(f.state, "name", f.state) or "")
            if state.endswith("ACTIVE"):
                return f
            if state.endswith("FAILED"):
                raise GeminiError("Gemini File API processing failed")
            if time.monotonic() > deadline:
                raise GeminiBusy("video", 0, "File API did not finish processing the upload in time")
            time.sleep(3)
            f = self.client.files.get(name=f.name)
