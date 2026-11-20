"""Gemini calls via the google-genai SDK (JSON mode + response schema)."""
from __future__ import annotations

import os
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


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _fmt_values(values: list[str]) -> str:
    return ", ".join(values) if values else "(none yet)"


class Gemini:
    def __init__(self, cfg: dict):
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise GeminiError("GEMINI_API_KEY not set")
        from google import genai

        self._genai = genai
        self.client = genai.Client(api_key=key)
        self.cfg = cfg["gemini"]
        self.system = _prompt("system.txt")

    # ------------------------------------------------------------------ helpers

    def _config(self):
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=self.system,
            response_mime_type="application/json",
            response_schema=Extraction,
            temperature=float(self.cfg["temperature"]),
        )

    @staticmethod
    def _is_quota(err: Exception) -> bool:
        code = getattr(err, "code", None)
        text = str(err)
        return code == 429 or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower()

    def _generate(self, contents: list) -> Extraction:
        """Call the model; retry once on invalid output. Raises QuotaExceeded on 429."""
        from google.genai import errors

        last: Exception | None = None
        for _ in range(2):
            try:
                resp = self.client.models.generate_content(
                    model=self.cfg["model"], contents=contents, config=self._config()
                )
            except errors.APIError as e:
                if self._is_quota(e):
                    raise QuotaExceeded(f"Gemini quota: {getattr(e, 'code', '')} {getattr(e, 'status', '')}".strip()) from None
                raise GeminiError(f"Gemini API {getattr(e, 'code', '')}: {(getattr(e, 'message', None) or str(e))[:160]}") from None
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
        return self._generate([text])

    def from_media(
        self,
        caption: str,
        media: list[MediaFile],
        missing: list[str],
        cuisines: list[str],
        types_: list[str],
    ) -> Extraction:
        from google.genai import types

        kind = "video" if any(m.mime.startswith("video/") for m in media) else "images"
        text = Template(_prompt("media.txt")).safe_substitute(
            caption=(caption or "").strip() or "(no caption)",
            media_kind=kind,
            missing=" and ".join(missing) or "nothing",
            existing_cuisines=_fmt_values(cuisines),
            existing_types=_fmt_values(types_),
        )
        total = sum(m.size for m in media)
        inline_limit = int(self.cfg["inline_max_mb"]) * 1024 * 1024
        uploaded: list = []
        try:
            parts: list = []
            if total < inline_limit:
                for m in media:
                    parts.append(types.Part.from_bytes(data=m.path.read_bytes(), mime_type=m.mime))
            else:
                for m in media:
                    f = self.client.files.upload(file=str(m.path), config={"mime_type": m.mime})
                    uploaded.append(f)
                    parts.append(self._wait_active(f))
            return self._generate(parts + [text])
        finally:
            for f in uploaded:
                try:
                    self.client.files.delete(name=f.name)
                except Exception:
                    pass

    def _wait_active(self, f):
        deadline = time.monotonic() + int(self.cfg["file_poll_timeout_s"])
        while True:
            state = str(getattr(f.state, "name", f.state) or "")
            if state.endswith("ACTIVE"):
                return f
            if state.endswith("FAILED"):
                raise GeminiError("Gemini File API processing failed")
            if time.monotonic() > deadline:
                raise GeminiError("Gemini File API timed out waiting for ACTIVE")
            time.sleep(3)
            f = self.client.files.get(name=f.name)
