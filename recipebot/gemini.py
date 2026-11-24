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
    """Gemini 429 / RESOURCE_EXHAUSTED on every configured model: stop the run, keep items queued."""


class GeminiError(Exception):
    pass


class ModelUnavailable(GeminiError):
    """Every configured model is unknown / retired: stop the run, keep items queued."""


class GeminiBusy(GeminiError):
    """Every usable model stayed overloaded / timed out through every retry."""

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


def _fmt_comments(comments: list[str] | None) -> str:
    items = [c.strip() for c in (comments or []) if c and c.strip()]
    return "\n---\n".join(items)[:4000] if items else "(none)"


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


def quota_scope(err: Exception) -> tuple[str, float | None]:
    """("day" | "minute" | "unknown", retry delay in seconds) for a 429 error.

    The error details name the quota that ran out (e.g. "...PerDay..." or "...PerMinute...")
    and usually carry a RetryInfo delay. A per-minute limit clears within a minute, so only
    a per-day limit means the model is used up for the rest of the run.
    """
    details = getattr(err, "details", None)
    try:
        text = json.dumps(details, default=str) if details else str(err)
    except (TypeError, ValueError):
        text = str(err)
    m = re.search(r'retryDelay"?\s*:\s*"?(\d+(?:\.\d+)?)s', text)
    delay = float(m.group(1)) if m else None
    if re.search(r"PerDay|per day|daily", text, re.I):
        return "day", delay
    if re.search(r"PerMinute|per minute", text, re.I):
        return "minute", delay
    return "unknown", delay


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
        # Models taken out for the rest of the run: model -> "quota" | "gone".
        self._dead: dict[str, str] = {}
        self.deadline = deadline  # time.monotonic() value; busy waits never run past it
        self._waits = [float(w) for w in (self.cfg.get("busy_waits_s") or [])]
        self._thinking = str(self.cfg.get("thinking_level") or "").strip().upper() or None
        self._light = bool(self.cfg.get("light_retry", True))
        # Free-tier pacing: minimum gap between requests, and waits for per-minute 429s.
        self._min_interval = float(self.cfg.get("min_interval_s") or 0)
        self._rpm_waits = [float(w) for w in (self.cfg.get("rpm_waits_s") or [])]
        self._last_call = 0.0
        # Optional settings switched off per model after that model rejected them (HTTP 400).
        self._no_thinking: set[str] = set()
        self._no_light: set[str] = set()
        self.busy_retries = 0  # reset per item (start_item); for logs and comments
        self.used: list[str] = []  # models that answered for the current item
        self.run_used: list[str] = []  # models that answered during the whole run

    def start_item(self) -> None:
        """Reset the per-item counters (busy retries, models used)."""
        self.busy_retries = 0
        self.used = []

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

    def _config(self, model: str, light: bool = False, step: str = ""):
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
        if self._thinking and model not in self._no_thinking:
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=self._thinking)
        if light and self._light and model not in self._no_light:
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

    def _drop_unsupported(self, err: Exception, model: str, light: bool) -> bool:
        """On a 400, switch off one optional setting for this model and report True (call again).

        The exact wording of "setting not supported" errors is not documented, so any 400
        counts: first the thinking level goes, then low media resolution. Once both are off
        a 400 is a real error. This way an unsupported setting can never block extraction.
        Settings are dropped per model, so an older fallback model does not switch them off
        for the newer ones.
        """
        if getattr(err, "code", None) != 400:
            return False
        msg = (getattr(err, "message", None) or str(err))[:160]
        if self._thinking and model not in self._no_thinking:
            _log(status="gemini-setting-dropped", setting="thinking_level", model=model, error=msg)
            self._no_thinking.add(model)
            return True
        if light and self._light and model not in self._no_light:
            _log(status="gemini-setting-dropped", setting="media_resolution", model=model, error=msg)
            self._no_light.add(model)
            return True
        return False

    # ------------------------------------------------------------------ fallback chain

    def _usable(self) -> list[int]:
        """Indexes of the models still in play this run, in the configured order."""
        return [i for i, m in enumerate(self.models) if m not in self._dead]

    def _retire(self, reason: str, err: Exception) -> None:
        """Take the current model out for the rest of the run and move to the next usable one.

        reason is "quota" (429, its quota is used up) or "gone" (not found / not supported).
        Raises QuotaExceeded or ModelUnavailable when no model is left.
        """
        model = self.model
        self._dead[model] = reason
        left = self._usable()
        if not left:
            if "quota" in self._dead.values():
                raise QuotaExceeded(
                    f"Gemini quota used up on every model ({', '.join(self.models)}): {_short(err)}"
                ) from None
            raise ModelUnavailable(
                f"Gemini model(s) unavailable: {', '.join(self.models)}. "
                "Set the repo variable GEMINI_MODEL (see: python -m recipebot --list-models)"
            ) from None
        self._active = left[0]
        _log(status="gemini-fallback", reason=reason, from_model=model, to_model=self.model,
             error=_short(err))

    def _pace(self) -> None:
        """Keep at least min_interval_s between two requests (free-tier requests per minute)."""
        gap = self._last_call + self._min_interval - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.monotonic()

    def _rate_wait(self, err: Exception, tries: int, step: str) -> bool:
        """For a per-minute 429: wait and report True (call the same model again).

        Returns False for a daily quota (or once the per-minute waits are used up), so the
        caller takes the model out. Raises GeminiBusy when the run has no time left to wait,
        which keeps the item queued without wrongly retiring the model.
        """
        scope, delay = quota_scope(err)
        if scope == "day" or tries >= len(self._rpm_waits):
            return False
        wait = max(self._rpm_waits[tries], min(delay or 0.0, 90.0)) + random.uniform(0, 3)
        if self.deadline is not None and time.monotonic() + wait + _MIN_CALL_S > self.deadline:
            raise GeminiBusy(step, self.busy_retries, f"rate limited ({_short(err)}), no time left in this run")
        _log(status="gemini-rate-limit", step=step, model=self.model, scope=scope, wait_s=int(wait),
             retry=tries + 1, of=len(self._rpm_waits))
        time.sleep(wait)
        return True

    def _note_used(self, model: str) -> None:
        for lst in (self.used, self.run_used):
            if model not in lst:
                lst.append(model)

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
        """generate_content over the fallback chain.

        - quota used up (429) or model gone: that model is out for the rest of the run,
          the next one is tried at once.
        - busy (503, 5xx, timeout): the next usable model that has not been busy since the
          last wait is tried at once. Only when every usable model was busy does the call
          wait (busy_waits_s) and start again from the best usable model.
        The model that answers stays the active one for the following calls.
        """
        from google.genai import errors

        attempt = 0  # busy waits done in this call
        rate_tries = 0  # per-minute 429 waits done for the current model
        busy: set[str] = set()  # models that were busy since the last wait
        while True:
            # After a busy wait, media requests ask for low media resolution (far fewer tokens).
            light = has_media and attempt > 0
            model = self.model
            try:
                config = self._config(model, light, step)
                self._pace()
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
                self._note_used(model)
                return resp
            except errors.APIError as e:
                if self._is_quota(e):
                    if self._rate_wait(e, rate_tries, step):
                        rate_tries += 1
                        continue
                    self._retire("quota", e)
                    rate_tries = 0
                    continue
                if self._is_model_gone(e):
                    self._retire("gone", e)
                    continue
                if self._drop_unsupported(e, model, light):
                    continue
                if not is_busy(e):
                    raise GeminiError(f"Gemini API {getattr(e, 'code', '')}: {(getattr(e, 'message', None) or str(e))[:160]}") from None
                err: Exception = e
            except Exception as e:  # network errors and timeouts raised below the SDK
                if not is_busy(e):
                    raise
                err = e
            busy.add(model)
            nxt = next((i for i in self._usable() if self.models[i] not in busy), None)
            if nxt is not None:
                self._active = nxt
                self.busy_retries += 1
                _log(status="gemini-fallback", reason="busy", step=step, from_model=model,
                     to_model=self.model, error=_short(err))
                continue
            self._pause(attempt, step, err)
            attempt += 1
            busy.clear()
            self._active = self._usable()[0]

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

    def from_caption(self, caption: str, cuisines: list[str], types_: list[str],
                     comments: list[str] | None = None) -> Extraction:
        text = Template(_prompt("caption.txt")).safe_substitute(
            caption=caption.strip() or "(no caption)",
            comments=_fmt_comments(comments),
            existing_cuisines=_fmt_values(cuisines),
            existing_types=_fmt_values(types_),
        )
        return self._generate([text], "caption")

    def from_web(self, page_text: str, title: str, url: str, caption: str,
                 cuisines: list[str], types_: list[str]) -> Extraction:
        """Read the recipe from the text of the creator's web page (text only, no media)."""
        text = Template(_prompt("web.txt")).safe_substitute(
            page=page_text.strip(),
            title=title.strip() or "(no title)",
            url=url,
            caption=(caption or "").strip()[:1500] or "(no caption)",
            existing_cuisines=_fmt_values(cuisines),
            existing_types=_fmt_values(types_),
        )
        return self._generate([text], "web")

    def from_media(
        self,
        caption: str,
        media: list[MediaFile],
        missing: list[str],
        cuisines: list[str],
        types_: list[str],
        comments: list[str] | None = None,
    ) -> Extraction:
        kind = media_kind(media)
        text = Template(_prompt("media.txt")).safe_substitute(
            caption=(caption or "").strip() or "(no caption)",
            comments=_fmt_comments(comments),
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
