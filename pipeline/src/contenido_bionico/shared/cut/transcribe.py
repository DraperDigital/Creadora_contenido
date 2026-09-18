"""Transcribe a local MP4 / audio file with ElevenLabs Scribe.

Called as a subprocess from `cut/orchestrator.py` on the raw MP4 to produce
`_intermediates/raw_transcript.json`, which feeds the editor + reviewer +
mapper + trim_spacings chain. The final per-run `transcript.json` is NOT
re-transcribed — `derive_transcript.py` rebuilds it from word_for_word + the
EDL render plan.

Sanitization of ElevenLabs Scribe output (handled inside this helper):
  - Completeness check (`transcribe_with_completeness`): detects audio-vs-
    transcript truncation, retries, and if still short recovers the tail
    with overlap clips + anchor splice.
  - `resolve_audio_events`: re-runs Scribe on any multi-word `audio_event`
    token (clip + new call with diarize=False; fallback to linear
    interpolation).

Output JSON shape (consumed by `mapper.py` and `trim_spacings.py`):
    {
      "language_code": "spa",
      "language_probability": 0.97,
      "text": "complete transcript...",
      "words": [
        {"text": "Should", "start": 3.62, "end": 4.12, "type": "word",
         "speaker_id": "speaker_0", "logprob": -0.0001},
        {"text": " ", "start": 4.12, "end": 4.14, "type": "spacing",
         "speaker_id": "speaker_0"},
        ...
      ]
    }
Tokens come back in temporal order. `type` may be `word`, `spacing`, or
`audio_event` (the last one is sanitized above except for the linear-
interpolation fallback). `speaker_id` and `logprob` are optional.

Usage:
    python transcribe.py <input.mp4> -o <output.json>
    python transcribe.py <input.mp4> -o <output.json> --no-diarize

Requires `ELEVENLABS_API_KEY` in the environment. The key must include the
ElevenLabs Speech to Text permission.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from typing import Any

# Ensure `from contenido_bionico...` resolves when this file is invoked as a script.
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from contenido_bionico.shared import config  # noqa: E402

# Hide the console window when spawning subprocesses on Windows. No-op elsewhere.
from contenido_bionico.shared.ffmpeg import (  # noqa: E402
    NO_WINDOW as _NO_WIN,
    probe_duration_or_none,
)

# Force UTF-8 on stdout/stderr. Without this, on Windows the default codec
# (cp1252) crashes when printing non-ASCII characters (accents, arrows, ¿, ¡)
# and the subprocess aborts even though the real work already completed.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass

# Load .env from the repo root if present.
for _candidate_root in [Path(__file__).resolve().parents[4], Path(__file__).resolve().parents[5]]:
    _ENV = _candidate_root / ".env"
    if _ENV.exists():
        for line in _ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()



def extract_audio_to_mp3(video_path: Path, out_dir: Path) -> Path:
    """Extract audio from the input MP4 to MP3 192k with ffmpeg. Return MP3 path."""
    audio_path = out_dir / "audio.mp3"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(audio_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode})")
    if not audio_path.exists():
        raise RuntimeError("ffmpeg did not produce audio.mp3")
    return audio_path


_TRANSCRIPT_GAP_TOLERANCE_S = 10.0  # gap audio_end - last_word.end > this = truncated
_TAIL_RECOVERY_OVERLAPS_S = (10.0, 30.0)  # tier 2 and tier 3
_TAIL_ANCHOR_MIN_MATCHES = 3


def _get_audio_duration(path: Path) -> float | None:
    """Run ffprobe on the file and return duration in seconds. None on failure."""
    return probe_duration_or_none(path, timeout=30)


def _transcript_last_timestamp(words: list[dict[str, Any]]) -> float:
    """Largest `end` (or `start`) across all tokens."""
    best = 0.0
    for w in words:
        e = w.get("end")
        s = w.get("start")
        for v in (e, s):
            if v is not None:
                try:
                    fv = float(v)
                    if fv > best:
                        best = fv
                except (TypeError, ValueError):
                    pass
    return best


def _is_transient_scribe_error(exc: Exception) -> tuple[bool, float | None]:
    """Decide whether an exception from `transcribe_with_scribe` looks like a
    transient infrastructure issue we should retry, vs. a permanent error
    (auth, billing, malformed input) that retrying would only waste time on.

    Returns (is_transient, retry_after_seconds_hint). The hint is honored when
    the server provides a `Retry-After` header on a 429.
    """
    try:
        import httpx  # type: ignore
    except ImportError:
        # If httpx isn't importable, we can't classify HTTP errors precisely.
        # Fall back to "treat unknown as permanent" so we don't loop forever.
        return False, None

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or 500 <= status < 600:
            retry_after = None
            try:
                ra_raw = exc.response.headers.get("retry-after")
                if ra_raw is not None:
                    retry_after = float(ra_raw)
            except (TypeError, ValueError):
                retry_after = None
            return True, retry_after
        # 401/402/403/4xx other than 429 are permanent — don't retry.
        return False, None

    # Network-layer issues from httpx and the wider stdlib worth retrying.
    transient_types: tuple[type[BaseException], ...] = (
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        TimeoutError,
        ConnectionError,
    )
    if isinstance(exc, transient_types):
        return True, None

    return False, None


# Backoff schedule for retrying transient Scribe failures. 4 attempts total
# (initial + 3 retries) covers most observed ElevenLabs hiccups without making
# a permanent failure feel hung.
_SCRIBE_RETRY_DELAYS_S: tuple[float, ...] = (5.0, 15.0, 45.0)


def _scribe_call_only(audio_path: Path, api_key: str, diarize: bool) -> dict[str, Any]:
    """Call transcribe_with_scribe and return the dict; isolated here so the
    completeness flow can retry cleanly.

    Adds bounded retry on transient infrastructure errors (HTTP 429, HTTP 5xx,
    network errors). Permanent errors (auth, payment, bad input) bubble up on
    the first attempt so they fail fast in the orchestrator log instead of
    waiting for the full backoff schedule. Honors `Retry-After` on 429s.
    """
    import time as _time

    last_exc: Exception | None = None
    retry_after_override: float | None = None
    for attempt_idx, delay in enumerate((0.0,) + _SCRIBE_RETRY_DELAYS_S):
        if attempt_idx > 0:
            # Server-provided Retry-After REPLACES the scheduled backoff for
            # this attempt (capped to avoid pathological waits) — sleeping
            # both would double the wait on every rate-limited retry.
            wait = delay if retry_after_override is None else retry_after_override
            print(
                f"[transcribe] retry {attempt_idx}/{len(_SCRIBE_RETRY_DELAYS_S)} "
                f"in {wait:.0f}s after transient error: {last_exc}",
                flush=True,
            )
            _time.sleep(wait)
        try:
            return transcribe_with_scribe(audio_path, api_key, diarize=diarize)
        except Exception as exc:  # noqa: BLE001
            transient, retry_after_hint = _is_transient_scribe_error(exc)
            if not transient:
                raise
            last_exc = exc
            retry_after_override = None
            if retry_after_hint is not None:
                retry_after_override = max(1.0, min(retry_after_hint, 120.0))
                print(
                    f"[transcribe] honoring Retry-After: next retry in "
                    f"{retry_after_override:.0f}s",
                    flush=True,
                )
    # Exhausted retries — re-raise the last transient error.
    assert last_exc is not None
    raise last_exc


def _splice_tail_with_anchor(
    main_words: list[dict[str, Any]],
    tail_words_offset: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Splice the transcribed tail in `tail_words_offset` (timestamps already
    expressed in absolute source-audio time) onto `main_words` using the
    anchor pattern. Returns the merged list, or None if no anchor is found.

    Strategy: find a run of N consecutive words that appears identically in
    main_words (near its end) and in tail_words_offset (near its start).
    Truncate main_words at the anchor and concatenate with tail_words_offset
    from the anchor onward.
    """
    n = _TAIL_ANCHOR_MIN_MATCHES
    main_norms = [_norm_for_anchor(w.get("text", "")) for w in main_words]
    tail_norms = [_norm_for_anchor(w.get("text", "")) for w in tail_words_offset]

    # Restrict search in main_words to the last ~200 words (covers any
    # reasonable overlap up to 30s).
    main_search_lo = max(0, len(main_norms) - 200)
    # Restrict search in tail to the first ~200 words.
    tail_search_hi = min(len(tail_norms), 200)

    # Walk main backwards (we want the anchor closest to the end of main).
    for i in range(len(main_norms) - n, main_search_lo - 1, -1):
        seq = main_norms[i:i + n]
        if not all(seq):
            continue
        for j in range(0, tail_search_hi - n + 1):
            if tail_norms[j:j + n] == seq:
                # Splice: keep main[:i+n], take tail[j+n:]
                return main_words[:i + n] + tail_words_offset[j + n:]
    return None


def _attempt_tail_recovery(
    full_audio_path: Path,
    api_key: str,
    main_words: list[dict[str, Any]],
    audio_duration: float,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Try to recover a truncated tail with overlap clips (tier 2: 10s;
    tier 3: 30s). Returns (full_words | None, message).
    """
    last_ts = _transcript_last_timestamp(main_words)

    for overlap_s in _TAIL_RECOVERY_OVERLAPS_S:
        clip_start = max(0.0, last_ts - overlap_s)
        clip_end = audio_duration
        clip_dur = clip_end - clip_start
        if clip_dur <= 0:
            continue
        print(f"  tail recovery: clipping [{clip_start:.2f}, {clip_end:.2f}] "
              f"({clip_dur:.1f}s, overlap={overlap_s:.0f}s)...", flush=True)

        with tempfile.TemporaryDirectory() as td:
            clip_path = Path(td) / "tail.mp3"
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-i", str(full_audio_path),
                "-ss", str(clip_start), "-to", str(clip_end),
                "-c:a", "libmp3lame", "-b:a", "192k",
                str(clip_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
            if proc.returncode != 0 or not clip_path.exists():
                print(f"    ffmpeg clip failed", flush=True)
                continue

            try:
                clip_result = _scribe_call_only(clip_path, api_key, diarize=False)
            except Exception as e:  # noqa: BLE001
                print(f"    Scribe clip failed: {e}", flush=True)
                continue

        clip_words = clip_result.get("words", [])
        if not clip_words:
            continue

        # Offset clip timestamps to absolute source time.
        offset_words: list[dict[str, Any]] = []
        for cw in clip_words:
            ow = dict(cw)
            if ow.get("start") is not None:
                ow["start"] = round(float(ow["start"]) + clip_start, 3)
            if ow.get("end") is not None:
                ow["end"] = round(float(ow["end"]) + clip_start, 3)
            offset_words.append(ow)

        # Verify the clip extended things forward.
        clip_last_ts = _transcript_last_timestamp(offset_words)
        if clip_last_ts <= last_ts + 1.0:
            print(f"    clip didn't extend the transcript (clip last_ts={clip_last_ts:.2f}, "
                  f"main last_ts={last_ts:.2f})", flush=True)
            continue

        spliced = _splice_tail_with_anchor(main_words, offset_words)
        if spliced is None:
            print(f"    no anchor found in overlap; widening", flush=True)
            continue

        new_last_ts = _transcript_last_timestamp(spliced)
        new_gap = audio_duration - new_last_ts
        if new_gap > _TRANSCRIPT_GAP_TOLERANCE_S:
            print(f"    splice succeeded but transcript still truncated "
                  f"(gap={new_gap:.2f}s); widening", flush=True)
            continue

        print(f"    tail recovery OK with overlap={overlap_s:.0f}s; "
              f"new last_ts={new_last_ts:.2f}s, gap={new_gap:.2f}s", flush=True)
        return spliced, f"recovered with {overlap_s:.0f}s overlap"

    return None, "all tail recovery tiers failed"


def transcribe_with_completeness(
    audio_path: Path, api_key: str, diarize: bool = True
) -> dict[str, Any]:
    """Wrap `transcribe_with_scribe` with a completeness check.

    Tiers:
      1. Initial call. If audio-vs-transcript gap <= tolerance, OK.
      2. Otherwise retry full transcription once.
      3. If still truncated, recover the tail with a 10s overlap clip +
         anchor splice.
      4. If still truncated, recover with a 30s overlap clip.
      5. If still truncated, log a loud warning and return what we have so
         the pipeline fails loudly downstream.
    """
    audio_dur = _get_audio_duration(audio_path)
    if audio_dur is None:
        print(f"WARN: could not get audio duration, skipping completeness check",
              flush=True)
        return _scribe_call_only(audio_path, api_key, diarize)

    # Tier 1: initial call
    result = _scribe_call_only(audio_path, api_key, diarize)
    last_ts = _transcript_last_timestamp(result.get("words", []))
    gap = audio_dur - last_ts
    if gap <= _TRANSCRIPT_GAP_TOLERANCE_S:
        print(f"completeness OK (audio={audio_dur:.2f}s, last_ts={last_ts:.2f}s, "
              f"gap={gap:.2f}s)", flush=True)
        return result

    print(f"WARN: transcript truncated (audio={audio_dur:.2f}s, "
          f"last_ts={last_ts:.2f}s, gap={gap:.2f}s); retrying full transcription...",
          flush=True)

    # Tier 2: full retry
    try:
        retry_result = _scribe_call_only(audio_path, api_key, diarize)
    except Exception as e:  # noqa: BLE001
        print(f"  full retry failed: {e}", flush=True)
        retry_result = None

    if retry_result is not None:
        retry_last_ts = _transcript_last_timestamp(retry_result.get("words", []))
        retry_gap = audio_dur - retry_last_ts
        if retry_gap <= _TRANSCRIPT_GAP_TOLERANCE_S:
            print(f"  full retry OK (last_ts={retry_last_ts:.2f}s, gap={retry_gap:.2f}s)",
                  flush=True)
            return retry_result
        # Use whichever has more content as the base for tail recovery.
        if retry_last_ts > last_ts:
            result = retry_result
            last_ts = retry_last_ts
            gap = retry_gap
        print(f"  retry still truncated (gap={retry_gap:.2f}s); attempting tail recovery...",
              flush=True)

    # Tier 3 & 4: tail recovery via anchor splice
    spliced, msg = _attempt_tail_recovery(
        audio_path, api_key, result.get("words", []), audio_dur
    )
    if spliced is not None:
        result["words"] = spliced
        return result

    # Tier 5: human review needed — emit a loud stderr warning so the pipeline
    # surfaces the problem downstream.
    final_last_ts = _transcript_last_timestamp(result.get("words", []))
    final_gap = audio_dur - final_last_ts
    alert = (
        f"WARN: transcript truncated — human review may be needed\n"
        f"  audio: {audio_path.name} ({audio_dur:.1f}s)\n"
        f"  transcript ends at: {final_last_ts:.1f}s\n"
        f"  missing: {final_gap:.1f}s\n"
        f"  reason: {msg}"
    )
    print(alert, file=sys.stderr, flush=True)
    raise RuntimeError(
        f"transcript truncated and unrecoverable: {audio_path.name} ends at "
        f"{final_last_ts:.1f}s of {audio_dur:.1f}s (missing {final_gap:.1f}s). {msg}"
    )


def _check_scribe_account(api_key: str) -> str:
    """Hit /v1/user/subscription and return a usage-vs-limit string,
    or an error message if the lookup fails. Never raises."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": api_key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        used = data.get("character_count", "?")
        limit = data.get("character_limit", "?")
        tier = data.get("tier", "?")
        next_reset = data.get("next_character_count_reset_unix")
        line = f"account: tier={tier} used={used} of {limit} chars"
        if isinstance(used, int) and isinstance(limit, int) and limit > 0:
            pct = (used / limit) * 100
            line += f" ({pct:.1f}%)"
        if next_reset:
            from datetime import datetime as _dt
            try:
                line += f" reset={_dt.fromtimestamp(int(next_reset)).strftime('%Y-%m-%d')}"
            except Exception:
                pass
        return line
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return f"account: HTTP {e.code} when checking — {body}"
    except Exception as e:  # noqa: BLE001
        return f"account: could not check ({type(e).__name__}: {e})"


def _diagnose_scribe_error(exc: Exception, api_key: str, audio_path: Path) -> str:
    """Turn a Scribe-call exception into a clear human-readable message.
    Detects exception type, HTTP status if applicable, response body if
    available, and appends an account-state check."""
    import httpx
    name = type(exc).__name__
    msg = str(exc)

    audio_size_mb = 0.0
    try:
        audio_size_mb = audio_path.stat().st_size / (1024 * 1024)
    except Exception:
        pass

    header = "Scribe API error"
    classification = ""
    detail = ""

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            body = exc.response.text[:500]
        except Exception:
            body = "(could not read response body)"
        if status == 401:
            classification = "AUTH ERROR (401): invalid or expired API key"
        elif status == 402:
            classification = "PAYMENT ERROR (402): account out of credits or payment required"
        elif status == 403:
            classification = "FORBIDDEN (403): account is not allowed for this endpoint or model"
        elif status == 429:
            classification = "RATE LIMITED (429): too many requests — wait and retry"
        elif status >= 500:
            classification = f"SERVER ERROR ({status}): ElevenLabs returned an internal error"
        else:
            classification = f"HTTP {status}"
        detail = f"server response body: {body}"

    elif isinstance(exc, httpx.RemoteProtocolError):
        classification = ("CONNECTION DROPPED: ElevenLabs closed the connection "
                          "before sending the full response (typical when the "
                          "server is overloaded or processing timed out). "
                          "Retry usually works.")
        detail = msg

    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        classification = ("CONNECTION ERROR: could not open a connection to "
                          "ElevenLabs (likely your network, not their API).")
        detail = msg

    elif isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        classification = ("TIMEOUT: the request took too long. Large audio or "
                          "slow server.")
        detail = msg

    elif isinstance(exc, httpx.RequestError):
        classification = f"NETWORK ERROR ({name}): {msg}"
        detail = ""

    else:
        classification = f"UNHANDLED ({name}): {msg}"
        detail = ""

    account = _check_scribe_account(api_key)

    parts = [
        header,
        classification,
        f"audio: {audio_path.name} ({audio_size_mb:.1f} MB)",
        account,
    ]
    if detail:
        parts.append(detail[:300])
    return "\n".join(parts)


def transcribe_with_scribe(
    audio_path: Path, api_key: str, diarize: bool = True
) -> dict[str, Any]:
    """Call ElevenLabs Scribe on an audio file. Return the canonical shape."""
    try:
        from elevenlabs import ElevenLabs
    except ImportError as e:
        raise RuntimeError(
            "elevenlabs package not installed. `pip install elevenlabs`"
        ) from e

    client = ElevenLabs(api_key=api_key)
    try:
        with open(audio_path, "rb") as f:
            response = client.speech_to_text.convert(
                file=f,
                model_id="scribe_v2",
                language_code="es",
                tag_audio_events=False,
                diarize=diarize,
                timestamps_granularity="word",
                # Run-to-run determinism. Scribe v2 accepts sampling
                # `temperature`; 0.0 means greedy decoding so retries on
                # the same audio return the same tokens. Important for
                # the mapper / editor / Finalizer chain — a fresh transcript
                # with slightly different boundaries would invalidate every
                # editor proposal cached on disk.
                temperature=0.0,
                # Keep verbatim disfluencies, false starts, and filler
                # words. The mapper aligns the editor's final.txt against
                # *exactly* what Scribe emitted; if Scribe silently cleaned
                # up disfluencies, the editor's removals (which assume the
                # noise is there) would no longer correspond to actual
                # source tokens. False = verbatim.
                no_verbatim=False,
            )
    except Exception as e:  # noqa: BLE001
        diag = _diagnose_scribe_error(e, api_key, audio_path)
        print(diag, file=sys.stderr, flush=True)
        raise

    words_out: list[dict[str, Any]] = []
    for w in (response.words or []):
        entry: dict[str, Any] = {
            "text": w.text,
            "start": float(w.start) if w.start is not None else None,
            "end": float(w.end) if w.end is not None else None,
            "type": w.type,
        }
        speaker = getattr(w, "speaker_id", None)
        if speaker is not None:
            entry["speaker_id"] = speaker
        logprob = getattr(w, "logprob", None)
        if logprob is not None:
            entry["logprob"] = logprob
        words_out.append(entry)

    return {
        "language_code": getattr(response, "language_code", None),
        "language_probability": getattr(response, "language_probability", None),
        "text": getattr(response, "text", ""),
        "words": words_out,
    }


def resolve_audio_events(
    full_audio_path: Path,
    words: list[dict[str, Any]],
    api_key: str,
) -> list[dict[str, Any]]:
    """Some Scribe calls lump a stretch of speech into a single audio_event
    token (multi-word text). Such tokens have NO per-word timestamps, so any
    downstream tool that selects time ranges (mapper, trim_spacings, render_edl)
    will pull the wrong audio.

    For each multi-word audio_event we re-transcribe just the corresponding
    audio clip with diarize=False (which often makes Scribe behave
    differently) and offset the resulting per-word timestamps by the clip
    start. If the re-transcription still produces a multi-word audio_event,
    we fall back to linear-interpolation splitting (best-effort, inaccurate).
    """
    out: list[dict[str, Any]] = []

    for w in words:
        if w.get("type") != "audio_event":
            out.append(w)
            continue
        text = (w.get("text") or "").strip()
        tokens = text.split()
        if len(tokens) <= 1:
            out.append(w)
            continue

        start = w.get("start")
        end = w.get("end")
        if start is None or end is None or end <= start:
            out.extend(_linear_split_audio_event(w))
            continue

        # Try clipping + re-Scribe.
        replacement: list[dict[str, Any]] | None = None
        with tempfile.TemporaryDirectory() as td:
            clip_path = Path(td) / "clip.mp3"
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-i", str(full_audio_path),
                "-ss", str(start), "-to", str(end),
                "-c:a", "libmp3lame", "-b:a", "192k",
                str(clip_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
            if proc.returncode == 0 and clip_path.exists():
                try:
                    print(f"  re-transcribing audio_event clip [{start:.2f}, {end:.2f}] "
                          f"(diarize=False)...", flush=True)
                    # Same transient-error retry wrapper as the main call —
                    # a single 429/5xx here should not silently drop the
                    # clip into the linear-interpolation fallback.
                    clip_result = _scribe_call_only(clip_path, api_key, diarize=False)
                    clip_words = clip_result.get("words", [])
                    # Bail out if the clip itself produced another multi-word audio_event.
                    bad = any(
                        cw.get("type") == "audio_event"
                        and len((cw.get("text") or "").split()) > 1
                        for cw in clip_words
                    )
                    if not bad and clip_words:
                        # Offset timestamps by `start`.
                        offset = float(start)
                        offset_words: list[dict[str, Any]] = []
                        for cw in clip_words:
                            ow = dict(cw)
                            if ow.get("start") is not None:
                                ow["start"] = round(float(ow["start"]) + offset, 3)
                            if ow.get("end") is not None:
                                ow["end"] = round(float(ow["end"]) + offset, 3)
                            offset_words.append(ow)
                        replacement = offset_words
                except Exception as e:  # noqa: BLE001
                    print(f"  re-transcribe failed: {e}", flush=True)

        if replacement is None:
            print(f"  audio_event re-transcribe yielded no usable result; "
                  f"falling back to linear interpolation", flush=True)
            out.extend(_linear_split_audio_event(w))
        else:
            n_words = sum(1 for x in replacement if x.get("type") == "word")
            print(f"  audio_event resolved into {n_words} words via re-transcription",
                  flush=True)
            out.extend(replacement)
    return out


def _linear_split_audio_event(w: dict[str, Any]) -> list[dict[str, Any]]:
    """Last-resort: split an audio_event into evenly-spaced synthetic words.
    Timestamps are NOT accurate; only used when re-transcription failed."""
    out: list[dict[str, Any]] = []
    text = (w.get("text") or "").strip()
    tokens = text.split()
    start = w.get("start")
    end = w.get("end")
    speaker = w.get("speaker_id")
    if not tokens or start is None or end is None or end <= start:
        out.append(w)
        return out
    cleaned = [_clean_audio_event_token(t) for t in tokens]
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        out.append(w)
        return out
    total_chars = sum(len(t) for t in cleaned) or 1
    cursor = float(start)
    duration = float(end) - float(start)
    for i, tok in enumerate(cleaned):
        tok_dur = duration * (len(tok) / total_chars)
        tok_start = cursor
        tok_end = cursor + tok_dur
        cursor = tok_end
        entry: dict[str, Any] = {
            "text": tok,
            "start": round(tok_start, 3),
            "end": round(tok_end, 3),
            "type": "word",
        }
        if speaker is not None:
            entry["speaker_id"] = speaker
        out.append(entry)
        if i < len(cleaned) - 1:
            out.append({
                "text": " ",
                "start": round(tok_end, 3),
                "end": round(tok_end, 3),
                "type": "spacing",
                **({"speaker_id": speaker} if speaker is not None else {}),
            })
    return out


def _clean_audio_event_token(token: str) -> str:
    """Remove `..` and `--` markers from a token that came from an
    audio_event blob. These markers indicate truncation in Scribe's
    per-word output but are noise in lumped audio_event text."""
    if not token:
        return ""
    # Drop trailing `..` (but not `...` which is a legit ellipsis).
    while token.endswith(".."):
        if token.endswith("..."):
            break
        token = token[:-2].rstrip()
        if not token:
            return ""
    # Drop `--` anywhere (always noise in this context).
    token = token.replace("--", "")
    return token.strip()


def _norm_for_anchor(text: str) -> str:
    """Normalize text for anchor matching: lowercase, no accents, no
    punctuation. Two "equal" tokens produce the same string. Used by
    `_splice_tail_with_anchor` to align the recovered tail onto the main
    transcript."""
    import unicodedata
    if not text:
        return ""
    t = "".join(
        c for c in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(c) != "Mn"
    )
    t = re.sub(r"[^\wáéíóúñü]", "", t)
    return t



def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe MP4 with ElevenLabs Scribe.")
    ap.add_argument("input", type=Path, help="MP4 (or audio) file to transcribe.")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output JSON path.")
    ap.add_argument(
        "--no-diarize",
        action="store_true",
        help="Disable speaker diarization (default: enabled).",
    )
    args = ap.parse_args()

    if not args.input.exists():
        print(f"ERROR: input does not exist: {args.input}", file=sys.stderr)
        return 1

    config.load_env_into_process()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # If the input is already audio (.mp3/.wav/.m4a) send it directly;
        # if it's a video, extract audio first.
        suffix = args.input.suffix.lower()
        if suffix in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
            audio_path = args.input
        else:
            print(f"extracting audio from {args.input.name}...")
            audio_path = extract_audio_to_mp3(args.input, tmp_dir)

        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            print(
                "ERROR: no ElevenLabs API key set. Run `contenido-bionico setup` "
                "or define ELEVENLABS_API_KEY in .env. The key must include "
                "the speech_to_text permission.",
                file=sys.stderr,
            )
            return 1
        # The video was already de-silenced up front (shared/desilence.py), so the
        # audio fed to Scribe has no over-long pauses to glue words across. Send it
        # straight through; the word timestamps are already on the working timeline.
        scribe_audio = audio_path
        print("calling ElevenLabs Scribe (with completeness check)...")
        result = transcribe_with_completeness(
            scribe_audio, api_key, diarize=not args.no_diarize
        )
        result["provider"] = "elevenlabs"
        result["model"] = "scribe_v2"

        # ── Sanitization of the Scribe transcript ──────────────────────
        # Everything stays inside the temp-dir so audio.mp3 remains
        # available for ffmpeg when clipping audio_event re-Scribes.

        # 1) Multi-word audio_event tokens → clip + re-transcribe.
        bad_events = [
            w for w in result["words"]
            if w.get("type") == "audio_event"
            and len((w.get("text") or "").split()) > 1
        ]
        if bad_events:
            print(f"detected {len(bad_events)} multi-word audio_event token(s); "
                  f"resolving via clip + re-transcription...")
            result["words"] = resolve_audio_events(scribe_audio, result["words"], api_key)

        # Rebuild `text` from the modified array. Without this, consumers
        # that read `text` (verbose.txt) see the original dirty version
        # while consumers that read `words[]` see the cleaned one, and the
        # aligner fails.
        result["text"] = "".join(w.get("text", "") for w in result["words"])

    n_words = sum(1 for w in result["words"] if w.get("type") == "word")
    print(f"got {n_words} words ({len(result['words'])} tokens incl. spacing).")

    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
