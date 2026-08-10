"""ElevenLabs text-to-speech for the ranking (tier-list) pipeline.

Synthesizes the synthetic voice that announces the ranking topic (intro) and
each item name, and overlays the clips onto the recorded silences before the
creator speaks (pause detection + overlay live at the bottom of this module).

Reuses the SAME `ElevenLabs` client object as `shared/cut/transcribe.py`, but
calls `client.text_to_speech.convert(...)` (the TTS endpoint) instead of
`speech_to_text.convert`. The key must include the ElevenLabs **text_to_speech**
permission (the transcription path only needs speech_to_text).

Environment (`ELEVENLABS_API_KEY`, `BIONICO_TTS_VOICE_ID`, `BIONICO_TTS_MODEL`)
is read at CALL time, never at import time: the repo `.env` is loaded lazily
through `shared.config.load_env_into_process()` the moment a function needs a
value, so importing this module early (before config/env setup) can never
freeze stale values. (The archived edition parsed `.env` at import time — a
known bug this port fixes.)

Output:
    out_dir/intro.mp3
    out_dir/item_00.mp3 ... item_NN.mp3
    out_dir/tts_manifest.json   (the tts_manifest data contract)

`tts_manifest.json` shape (consumed by the overlay functions below and the
transcript builder):
    [
      {"slot": "intro", "text": "El ranking de hoy: ...", "file": "intro.mp3",
       "duration_s": 3.41},
      {"slot": 0, "name": "Python", "text": "Python.", "file": "item_00.mp3",
       "duration_s": 1.02},
      ...
    ]

Clips are cached: an existing non-empty mp3 is reused unless `force=True`.
ElevenLabs TTS is not bit-for-bit deterministic, so the cache is what keeps a
re-run from shifting every overlay offset.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ensure `from contenido_bionico...` resolves when this file is invoked as a
# script (mirrors the other shared helpers run by path as child processes).
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from contenido_bionico.shared.ffmpeg import (  # noqa: E402
    NO_WINDOW as _NO_WIN,
    VOICE_TARGET_LUFS,
    measure_lufs,
    probe_duration,
    probe_duration_or_none,
    static_gain_db,
)

# Default ElevenLabs voice: from the shared voice library, voiceId
# HMCmDsbKeaSZp5LMOYKR. Overridable per run via Ranking_Spec.json ->
# spec["voice"]["voice_id"], or via the BIONICO_TTS_VOICE_ID env variable.
# Previous defaults / other options: "Carolina" (UOIqAnmS11Reiei1Ytkc,
# peninsular ES), "Arconte" (QtPMrakdgePQIUwOX7Ut, deep male), "Clara"
# (LudcwvHIZaqQOcQfVZSY, formal).
DEFAULT_VOICE_ID = "HMCmDsbKeaSZp5LMOYKR"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"

# Spoken-line templates (overridable via spec["voice"]).
# Intro is kept SHORT so it fits the (often very short) opening silence: just the
# topic, no preamble. Override per run via spec["voice"]["intro_template"].
DEFAULT_INTRO_TEMPLATE = "{topic}."
DEFAULT_ITEM_TEMPLATE = "{name}."

# ElevenLabs TTS output format: 44.1 kHz, 128 kbps MP3 (matches the rest of the
# pipeline's 192k-or-lower mp3 intermediates closely enough; 128k is the
# standard tier-available format).
_OUTPUT_FORMAT = "mp3_44100_128"

# Bounded retry for the ElevenLabs synthesis call: transient failures (network
# hiccups, throttling, an empty response) are retried with exponential backoff,
# then the error is raised. Never retries forever.
_MAX_SYNTH_ATTEMPTS = 5
_BACKOFF_CAP_S = 30.0


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


def _resolve_api_key(api_key: str | None) -> str:
    """Return the ElevenLabs API key: explicit argument wins, else env (call time)."""
    if api_key:
        return api_key
    _load_env()
    key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set. TTS synthesis needs an ElevenLabs "
            "key with the text_to_speech permission (set it in the repo .env)."
        )
    return key


# --- Spanish spell-for-speech ------------------------------------------------
# ElevenLabs reads digits/symbols inconsistently (and often in the wrong
# language). For the narration we want the SPOKEN line to be plain Spanish
# words: "Seedance 2.0" -> "Seedance dos punto cero". `spell_for_speech` is the
# deterministic speller applied to every line we synthesize (intro + items) so
# the cache key, the synthesized audio, and the manifest `text` all agree.

_SPELL_UNITS = (
    "cero", "uno", "dos", "tres", "cuatro",
    "cinco", "seis", "siete", "ocho", "nueve",
)
# 10..19 and the irregular 20s are spelled out one-word in Spanish.
_SPELL_TEENS = {
    10: "diez", 11: "once", 12: "doce", 13: "trece", 14: "catorce",
    15: "quince", 16: "dieciséis", 17: "diecisiete", 18: "dieciocho",
    19: "diecinueve", 20: "veinte", 21: "veintiuno", 22: "veintidós",
    23: "veintitrés", 24: "veinticuatro", 25: "veinticinco",
    26: "veintiséis", 27: "veintisiete", 28: "veintiocho", 29: "veintinueve",
}
_SPELL_TENS = {
    3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta",
    7: "setenta", 8: "ochenta", 9: "noventa",
}
_SPELL_HUNDREDS = {
    1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos",
    5: "quinientos", 6: "seiscientos", 7: "setecientos", 8: "ochocientos",
    9: "novecientos",
}


def _spell_under_100(n: int) -> str:
    """Spanish words for 0..99."""
    if n < 10:
        return _SPELL_UNITS[n]
    if n < 30:
        return _SPELL_TEENS[n]
    tens, ones = divmod(n, 10)
    word = _SPELL_TENS[tens]
    if ones:
        return f"{word} y {_SPELL_UNITS[ones]}"
    return word


def _spell_under_1000(n: int) -> str:
    """Spanish words for 0..999."""
    if n < 100:
        return _spell_under_100(n)
    hundreds, rest = divmod(n, 100)
    if hundreds == 1 and rest == 0:
        return "cien"
    head = _SPELL_HUNDREDS[hundreds]
    if rest:
        return f"{head} {_spell_under_100(rest)}"
    return head


def _spell_int(n: int) -> str:
    """Spanish words for a non-negative integer 0..9999 (reasonable range)."""
    if n < 0:
        return "menos " + _spell_int(-n)
    if n < 1000:
        return _spell_under_1000(n)
    thousands, rest = divmod(n, 1000)
    if thousands == 1:
        head = "mil"
    else:
        head = f"{_spell_under_1000(thousands)} mil"
    if rest:
        return f"{head} {_spell_under_1000(rest)}"
    return head


# Digit runs, optionally with a single decimal point ("2", "2.0", "3.5").
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# Punctuation kept as spoken words when it sits between digits.
_KEPT_PUNCT = {".": "punto", ",": "coma", "&": "y"}


def _spell_number(token: str) -> str:
    """Spell a numeric token: integer run, or `<int> punto <digit words>`."""
    if "." in token:
        whole, frac = token.split(".", 1)
        whole_words = _spell_int(int(whole)) if whole else "cero"
        frac_words = " ".join(_SPELL_UNITS[int(d)] for d in frac)
        return f"{whole_words} punto {frac_words}"
    return _spell_int(int(token))


def spell_for_speech(text: str) -> str:
    """Render `text` as plain Spanish words for TTS narration.

    Deterministic speller for the SPOKEN line only (never the card `name`):

    - integer runs become Spanish number words ("20" -> "veinte"),
    - a decimal becomes "<intword> punto <digit-by-digit words>"
      ("2.0" -> "dos punto cero", "3.5" -> "tres punto cinco"),
    - "&" -> "y", "," -> "coma", and a "." between digits -> "punto",
    - other stray punctuation is dropped,
    - alphabetic words pass through untouched.

    Whitespace is collapsed so dropped symbols don't leave double spaces.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        m = _NUMBER_RE.match(text, i)
        if m:
            out.append(_spell_number(m.group(0)))
            i = m.end()
            continue
        if ch == "&":
            out.append(" y ")
        elif ch == ",":
            out.append(" coma ")
        elif ch.isalpha() or ch.isspace():
            out.append(ch)
        # Any other character (".", "-", "/", "(", etc.) outside a number is
        # dropped; surrounding whitespace is normalized below.
        i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def synthesize_line(
    text: str,
    out_path: Path,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    model_id: str = DEFAULT_MODEL_ID,
    api_key: str | None = None,
) -> Path:
    """Synthesize one spoken line to `out_path` (mp3). Return the path.

    Streams the ElevenLabs `text_to_speech.convert` byte iterator to disk. The
    convert call returns an iterator of audio chunks; we join them and write
    once so a partial network read never leaves a truncated playable file at
    `out_path`.

    `api_key=None` resolves the key from the environment at call time.
    Transient synthesis failures are retried up to `_MAX_SYNTH_ATTEMPTS` times
    with exponential backoff, then raised.
    """
    try:
        from elevenlabs import ElevenLabs
    except ImportError as exc:
        raise RuntimeError(
            "elevenlabs package not installed. `pip install elevenlabs`"
        ) from exc

    client = ElevenLabs(api_key=_resolve_api_key(api_key))
    for attempt in range(1, _MAX_SYNTH_ATTEMPTS + 1):
        try:
            audio = client.text_to_speech.convert(
                voice_id=voice_id,
                model_id=model_id,
                text=text,
                output_format=_OUTPUT_FORMAT,
            )
            # `convert` returns an Iterator[bytes]; accumulate then write atomically.
            chunks = bytearray()
            for chunk in audio:
                if chunk:
                    chunks.extend(chunk)
            if not chunks:
                raise RuntimeError(
                    f"ElevenLabs TTS returned no audio for text {text!r} "
                    f"(voice_id={voice_id}, model_id={model_id})."
                )
            break
        except Exception as exc:  # noqa: BLE001 - bounded retry, then raise
            if attempt >= _MAX_SYNTH_ATTEMPTS:
                raise RuntimeError(
                    f"ElevenLabs TTS failed after {_MAX_SYNTH_ATTEMPTS} attempts "
                    f"for text {text!r}: {exc}"
                ) from exc
            wait_s = min(_BACKOFF_CAP_S, 2.0 ** (attempt - 1))
            print(
                f"[tts] attempt {attempt}/{_MAX_SYNTH_ATTEMPTS} failed ({exc}); "
                f"retrying in {wait_s:.0f}s",
                flush=True,
            )
            time.sleep(wait_s)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    tmp.write_bytes(bytes(chunks))
    os.replace(tmp, out_path)
    return out_path


def _voice_config(spec: dict[str, Any]) -> dict[str, str]:
    """Resolve voice settings + line templates from the spec, with defaults.

    Priority per field: spec["voice"] override -> BIONICO_TTS_VOICE_ID /
    BIONICO_TTS_MODEL environment (read at CALL time) -> module default. With
    no spec override and no env set, behavior is identical to the archived
    edition (hardcoded defaults).
    """
    voice = spec.get("voice") or {}
    if not isinstance(voice, dict):
        voice = {}
    _load_env()
    env_voice_id = (os.environ.get("BIONICO_TTS_VOICE_ID") or "").strip()
    env_model_id = (os.environ.get("BIONICO_TTS_MODEL") or "").strip()
    return {
        "voice_id": str(voice.get("voice_id") or env_voice_id or DEFAULT_VOICE_ID),
        "model_id": str(voice.get("model_id") or env_model_id or DEFAULT_MODEL_ID),
        "intro_template": str(voice.get("intro_template") or DEFAULT_INTRO_TEMPLATE),
        "item_template": str(voice.get("item_template") or DEFAULT_ITEM_TEMPLATE),
    }


def _is_nonempty(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def synthesize_ranking_narration(
    spec: dict[str, Any],
    out_dir: Path,
    *,
    api_key: str | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Synthesize the intro + per-item narration for a ranking spec.

    Returns the `tts_manifest`: one `{"slot": "intro", ...}` entry followed by
    one `{"slot": <int index>, "name": ..., ...}` entry per item, in order.
    Also writes `out_dir/tts_manifest.json`.

    `spec` must carry `topic` (str) and `items` (list of `{"name": ...}`).
    Voice settings + templates come from `spec["voice"]` (all optional).
    `api_key=None` resolves ELEVENLABS_API_KEY from the environment per call.

    Clips are cached: an existing non-empty mp3 is reused unless `force=True`.
    Durations are probed with `probe_duration_or_none`; a clip whose duration
    cannot be probed is regenerated once before failing loud, since downstream
    overlaying needs an exact length.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    topic = str(spec.get("topic") or "").strip()
    if not topic:
        raise ValueError("Ranking_Spec.json: missing or empty 'topic'.")
    items = spec.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValueError("Ranking_Spec.json: 'items' must be a non-empty list.")

    cfg = _voice_config(spec)
    voice_id = cfg["voice_id"]
    model_id = cfg["model_id"]

    manifest: list[dict[str, Any]] = []

    def _emit(text: str, file_name: str, *, base: dict[str, Any]) -> None:
        """Synthesize (or reuse cached) `text` -> out_dir/file_name; append entry."""
        out_path = out_dir / file_name
        if force or not _is_nonempty(out_path):
            print(f"[tts] synthesizing {file_name}: {text!r}", flush=True)
            synthesize_line(
                text, out_path, voice_id=voice_id, model_id=model_id, api_key=api_key
            )
        else:
            print(f"[tts] reusing cached {file_name}", flush=True)
        duration = probe_duration_or_none(out_path)
        if duration is None:
            # A cached clip that won't probe is corrupt; regenerate once.
            print(f"[tts] could not probe {file_name}; regenerating", flush=True)
            synthesize_line(
                text, out_path, voice_id=voice_id, model_id=model_id, api_key=api_key
            )
            duration = probe_duration_or_none(out_path)
        if duration is None:
            raise RuntimeError(
                f"could not determine duration of TTS clip {out_path}; "
                "ffprobe failed on a freshly synthesized file."
            )
        entry = dict(base)
        entry["text"] = text
        entry["file"] = file_name
        entry["duration_s"] = round(float(duration), 3)
        manifest.append(entry)

    # The card/topic strings keep their original spelling; only the SPOKEN line
    # text is run through the Spanish speller (digits/symbols -> words).
    intro_text = spell_for_speech(cfg["intro_template"].format(topic=topic))
    _emit(intro_text, "intro.mp3", base={"slot": "intro"})

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"Ranking_Spec.json: items[{index}] must be an object with a 'name'."
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(
                f"Ranking_Spec.json: items[{index}] is missing a non-empty 'name'."
            )
        # `name` stays original for the card; only the spoken text is spelled.
        item_text = spell_for_speech(cfg["item_template"].format(name=name))
        _emit(item_text, f"item_{index:02d}.mp3", base={"slot": index, "name": name})

    manifest_path = out_dir / "tts_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[tts] wrote {manifest_path} ({len(manifest)} entries)", flush=True)

    # The on-disk manifest keeps `file` as a bare name (the data contract). The
    # in-memory copy carries an absolute `_abs_file` so the overlay stage can
    # locate each clip without re-deriving the TTS directory; the key is
    # underscore-prefixed and is intentionally NOT written to the JSON above.
    for entry in manifest:
        entry["_abs_file"] = str((out_dir / entry["file"]).resolve())
    return manifest


# ==========================================================================
# TTS placement + overlay. WHERE each announcement goes is decided by the LLM
# slot picker (short/ranking/agents/slot_picker.md, via the orchestrator) — there
# is NO deterministic pause detection here. These functions just take the picked
# windows, fit each clip into its silence, and mix it onto the audio (video
# untouched). None of this decides the cards — the tier board (which card, which
# lane, the animation) is authored entirely by the LLM author.
# ==========================================================================


def compute_tts_placements(
    slot_mapping: list[dict[str, Any]],
    tts_manifest: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Place each item's TTS in the tail of its pause, ending where speech resumes.

    Item `k`'s clip ends at the end of pause `k`. The OPENING pause (item 0) also
    hosts the intro clip, ending right before item 0. Returns, in time order,
    `[{label, name?, text?, clip, tts_start, tts_end, overflow_s}]`.
    """
    by_slot: dict[Any, dict[str, Any]] = {e.get("slot"): e for e in tts_manifest}

    def _resolve(label: Any) -> dict[str, Any]:
        clip = by_slot.get(label)
        if clip is None:
            raise ValueError(f"compute_tts_placements: no TTS clip for slot {label!r}")
        path = clip.get("_abs_file")
        if not path or not Path(path).exists():
            raise FileNotFoundError(
                f"compute_tts_placements: TTS clip missing for slot {label!r}: {path!r}."
            )
        return clip

    def _entry(label: Any, tts: dict[str, Any], end: float, pause_start: float):
        dur = float(tts["duration_s"])
        start = max(pause_start, end - dur)
        overflow = max(0.0, dur - (end - pause_start))
        entry: dict[str, Any] = {
            "label": label,
            "clip": str(tts["_abs_file"]),
            "tts_start": round(start, 3),
            "tts_end": round(start + dur, 3),
            "overflow_s": round(overflow, 3),
        }
        if "name" in tts:
            entry["name"] = tts["name"]
        if "text" in tts:
            entry["text"] = tts["text"]
        return entry, start

    placements: list[dict[str, Any]] = []
    for slot in slot_mapping:
        k = slot["label"]
        pause_start = float(slot["start"])
        pause_end = float(slot["end"])
        item_entry, item_start = _entry(k, _resolve(k), pause_end, pause_start)
        placements.append(item_entry)
        if k == 0 and "intro" in by_slot:
            intro_entry, _ = _entry("intro", _resolve("intro"), item_start, pause_start)
            placements.append(intro_entry)

    placements.sort(key=lambda p: p["tts_start"])
    return placements


def time_stretch_audio(src: Path, factor: float, dst: Path) -> float:
    """Speed up `src` by `factor` (pitch-preserving, ffmpeg atempo) -> `dst`."""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if factor <= 1.0 + 1e-6:
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return probe_duration(dst)
    factor = min(2.0, max(0.5, float(factor)))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-i", str(src), "-filter:a", f"atempo={factor:.5f}",
        "-c:a", "libmp3lame", "-q:a", "2", str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(
            f"time_stretch_audio failed (exit {proc.returncode}): {proc.stderr[-500:]}"
        )
    return probe_duration(dst)


def fit_tts_to_pauses(
    slot_mapping: list[dict[str, Any]],
    tts_manifest: list[dict[str, Any]],
    *,
    max_tempo: float = 1.5,
    safety_gap_s: float = 0.08,
) -> list[dict[str, Any]]:
    """Speed up TTS clips by the SMALLEST factor (<= `max_tempo`) that fits silence.

    For each pause, the clip(s) that must play inside it are time-stretched only
    as much as needed to fit the pause length minus a small `safety_gap_s`. The
    opening pause fits intro + first item together under one shared factor. A
    no-op when they already fit. Mutates and returns `tts_manifest`.
    """
    by_slot: dict[Any, dict[str, Any]] = {e.get("slot"): e for e in tts_manifest}

    def _dur(key: Any) -> float:
        entry = by_slot.get(key)
        try:
            return float(entry["duration_s"]) if entry else 0.0
        except (KeyError, TypeError, ValueError):
            return 0.0

    factors: dict[Any, float] = {}
    for slot in slot_mapping:
        if not slot.get("has_pause"):
            continue
        k = slot["label"]
        avail = max(0.0, float(slot["end"]) - float(slot["start"]))
        budget = max(0.1, avail - safety_gap_s)
        if k == 0 and "intro" in by_slot:
            needed = _dur("intro") + _dur(0)
            if needed > budget:
                f = min(max_tempo, needed / budget)
                factors["intro"] = max(factors.get("intro", 1.0), f)
                factors[0] = max(factors.get(0, 1.0), f)
        else:
            needed = _dur(k)
            if needed > budget:
                f = min(max_tempo, needed / budget)
                factors[k] = max(factors.get(k, 1.0), f)

    for key, factor in factors.items():
        if factor <= 1.0 + 1e-3:
            continue
        entry = by_slot.get(key)
        if not entry:
            continue
        src = Path(entry.get("_abs_file") or "")
        if not src.exists():
            continue
        dst = src.with_name(f"{src.stem}_x{int(round(factor * 100))}{src.suffix}")
        new_dur = time_stretch_audio(src, factor, dst)
        entry["_abs_file"] = str(dst)
        entry["file"] = dst.name
        entry["duration_s"] = round(new_dur, 3)
        entry["tempo"] = round(float(factor), 3)
        print(
            f"[tts] sped up '{entry.get('name') or key}' x{factor:.2f} "
            f"-> {new_dur:.2f}s to fit its pause",
            flush=True,
        )
    return tts_manifest


def overlay_tts(video_in: Path, placements: list[dict[str, Any]], out_video: Path) -> None:
    """Copy the video, rebuild the audio = normalized voice + each delayed TTS clip."""
    video_in = Path(video_in)
    out_video = Path(out_video)
    out_video.parent.mkdir(parents=True, exist_ok=True)
    if not placements:
        raise ValueError("overlay_tts: no placements to mix")

    cmd: list[str] = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-i", str(video_in),
    ]
    for p in placements:
        cmd += ["-i", str(p["clip"])]

    voice_gain = static_gain_db(measure_lufs(video_in), VOICE_TARGET_LUFS)
    parts: list[str] = [f"[0:a]volume={voice_gain}dB[v0]"]
    mix_labels = ["[v0]"]
    for i, p in enumerate(placements):
        clip_gain = static_gain_db(measure_lufs(p["clip"]), VOICE_TARGET_LUFS)
        delay_ms = int(round(float(p["tts_start"]) * 1000.0))
        parts.append(f"[{i + 1}:a]volume={clip_gain}dB,adelay={delay_ms}:all=1[d{i}]")
        mix_labels.append(f"[d{i}]")
    parts.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:normalize=0:dropout_transition=0,"
        + "alimiter=limit=0.95[aout]"
    )
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "0:v", "-c:v", "copy",
        "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out_video),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
    if proc.returncode != 0 or not out_video.exists():
        raise RuntimeError(
            f"tts overlay failed (exit {proc.returncode}): {proc.stderr[-800:]}"
        )
