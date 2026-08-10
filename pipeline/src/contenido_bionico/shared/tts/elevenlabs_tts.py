"""ElevenLabs text-to-speech for the AI comment voiceover.

Turns a text string into an MP3 narrated by a Spanish female voice, using the
account's ELEVENLABS_API_KEY (already in .env for transcription). No new deps:
stdlib urllib only. Results are cached by content hash so identical text is
never billed twice.

Config (env, resolved at CALL time — never frozen at import time; the archived
edition read these into module constants at import, which silently ignored
values loaded from `.env` afterwards):
  BIONICO_TTS_VOICE_ID    explicit ElevenLabs voice id (wins if set)
  BIONICO_TTS_VOICE_NAME  voice name substring to match (default "Cristina Campos")
  BIONICO_TTS_MODEL       model id (default "eleven_multilingual_v2")

Transient HTTP failures (429/5xx, network errors) are retried a bounded number
of times with exponential backoff, then raised as TTSError.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.elevenlabs.io/v1"
FALLBACK_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah - present on every account

# Hard defaults used when the BIONICO_TTS_* env variables are unset.
_FALLBACK_MODEL = "eleven_multilingual_v2"
_FALLBACK_VOICE_NAME = "Cristina Campos"

# Pinned synthesis parameters, so identical text renders identically across
# runs and the output quality never silently drifts with API defaults:
# 192 kbps MP3 (the API's default is 128 kbps) and a fixed voice_settings
# block. Both are part of the cache hash below.
_OUTPUT_FORMAT = "mp3_44100_192"
_VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}

# Bounded retry for the synthesis POST: never retries forever.
_MAX_ATTEMPTS = 5
_BACKOFF_CAP_S = 30.0
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class TTSError(RuntimeError):
    pass


class TTSHTTPError(TTSError):
    """A non-retryable HTTP failure from the ElevenLabs API."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _load_env() -> None:
    """Best-effort: load the repo `.env` into os.environ (setdefault semantics).

    Routed through V5's config loader so the env parsing rules live in ONE
    place. Guarded: `shared.config` raises when the package does not run from
    an editable checkout, and this module must stay importable/runnable even
    then (values already present in os.environ still work).
    """
    try:
        from contenido_bionico.shared.config import load_env_into_process

        load_env_into_process()
    except Exception:  # noqa: BLE001 - env loading is best-effort by design
        pass


def _default_model() -> str:
    """BIONICO_TTS_MODEL from the environment (call time), or the fallback."""
    _load_env()
    return (os.environ.get("BIONICO_TTS_MODEL") or "").strip() or _FALLBACK_MODEL


def _default_voice_name() -> str:
    """BIONICO_TTS_VOICE_NAME from the environment (call time), or the fallback."""
    _load_env()
    return (
        (os.environ.get("BIONICO_TTS_VOICE_NAME") or "").strip()
        or _FALLBACK_VOICE_NAME
    )


def _key() -> str:
    _load_env()
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise TTSError("ELEVENLABS_API_KEY is not set in the environment (.env).")
    return key


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"xi-api-key": _key()})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def resolve_voice_id(voice: str | None = None) -> str:
    """Resolve a voice id from an explicit id, a name substring, or the default."""
    if voice and len(voice) >= 20 and " " not in voice:
        return voice  # already looks like an id
    _load_env()
    env_id = os.environ.get("BIONICO_TTS_VOICE_ID", "").strip()
    if env_id:
        return env_id
    want = (voice or _default_voice_name()).lower()
    try:
        voices = _get_json(f"{API}/voices").get("voices", [])
    except Exception:
        return FALLBACK_VOICE_ID
    for v in voices:  # name match
        if want in (v.get("name", "") or "").lower():
            return v["voice_id"]
    for v in voices:  # else first female
        if (v.get("labels", {}) or {}).get("gender") == "female":
            return v["voice_id"]
    return voices[0]["voice_id"] if voices else FALLBACK_VOICE_ID


def _post_audio(url: str, body: bytes) -> bytes:
    """POST a synthesis request with bounded retry; return the MP3 bytes.

    Transient failures (429/5xx, network errors) are retried with exponential
    backoff. A non-retryable HTTP failure raises TTSHTTPError (with .code) so
    the caller can decide whether a degraded retry makes sense.
    """
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": _key(),
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:240].decode("utf-8", "ignore")
            if exc.code in _RETRYABLE_HTTP and attempt < _MAX_ATTEMPTS:
                time.sleep(min(_BACKOFF_CAP_S, 2.0 ** (attempt - 1)))
                continue
            raise TTSHTTPError(exc.code, f"ElevenLabs TTS HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < _MAX_ATTEMPTS:
                time.sleep(min(_BACKOFF_CAP_S, 2.0 ** (attempt - 1)))
                continue
            raise TTSError(
                f"ElevenLabs TTS network error after {_MAX_ATTEMPTS} attempts: {exc}"
            )
    return b""


def synthesize(text: str, out_path, voice: str | None = None, model: str | None = None) -> Path:
    """Generate `out_path` (MP3) narrating `text`. Returns the path."""
    out_path = Path(out_path)
    text = (text or "").strip()
    if not text:
        raise TTSError("Empty text for the voiceover.")
    voice_id = resolve_voice_id(voice)
    model = model or _default_model()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(
        "|".join(
            [
                voice_id,
                model,
                _OUTPUT_FORMAT,
                json.dumps(_VOICE_SETTINGS, sort_keys=True),
                text,
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    cache = out_path.parent / f".tts_cache_{digest}.mp3"
    if cache.exists() and cache.stat().st_size > 0:
        out_path.write_bytes(cache.read_bytes())
        return out_path
    body = json.dumps(
        {"text": text, "model_id": model, "voice_settings": _VOICE_SETTINGS}
    ).encode("utf-8")
    try:
        audio = _post_audio(
            f"{API}/text-to-speech/{voice_id}?output_format={_OUTPUT_FORMAT}", body
        )
    except TTSHTTPError as exc:
        # The pinned 192 kbps MP3 needs a Creator-tier ElevenLabs plan; lower
        # tiers reject the output_format alone with a 4xx. Retry once with the
        # account-default format so a smaller plan degrades bitrate instead of
        # failing the whole job. (A genuine bad request fails again and the
        # second, equivalent error propagates.)
        if exc.code not in (400, 401, 403):
            raise
        audio = _post_audio(f"{API}/text-to-speech/{voice_id}", body)
    if not audio:
        raise TTSError("ElevenLabs returned empty audio.")
    out_path.write_bytes(audio)
    try:
        cache.write_bytes(audio)
    except OSError:
        pass
    return out_path


def probe_text_to_speech_permission() -> tuple[bool | None, str]:
    """Zero-cost probe: does ELEVENLABS_API_KEY carry the text_to_speech permission?

    Sends an intentionally invalid synthesis request (empty JSON body, no
    "text"). A key without the permission is rejected by auth (401/403
    missing_permissions) BEFORE validation; a key with it reaches validation
    and gets a non-auth 4xx. No credits are consumed either way.

    Returns (ok, detail): ok is True/False when the API answered, or None when
    it could not be reached (offline) — None is NOT a bad key. The detail is
    doctor-facing (Spanish).
    """
    try:
        key = _key()
    except TTSError:
        return False, "ELEVENLABS_API_KEY no esta en el .env"
    req = urllib.request.Request(
        f"{API}/text-to-speech/{FALLBACK_VOICE_ID}",
        data=b"{}",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            body = exc.read()[:240].decode("utf-8", "ignore")
            if "missing_permissions" in body:
                return False, "la clave existe pero NO tiene el permiso text_to_speech"
            return False, f"ElevenLabs rechazo la clave (HTTP {exc.code})"
        return True, ""  # auth cleared; the 4xx is just our invalid body
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, "sin conexion con ElevenLabs"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Synthesize an MP3 voiceover via ElevenLabs TTS.")
    ap.add_argument("text")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--voice", default=None)
    args = ap.parse_args(argv)
    path = synthesize(args.text, args.output, voice=args.voice)
    print("wrote", path, path.stat().st_size, "bytes | voice_id", resolve_voice_id(args.voice))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
