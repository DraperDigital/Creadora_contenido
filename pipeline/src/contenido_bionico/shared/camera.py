"""Deterministic, automatic camera movements (stdlib only, no external deps).

This is the SINGLE camera renderer for the whole project. short + long +
faceless assembly all build their base-layer ffmpeg chain through
``camera.source_chain(...)``.

The camera is DETERMINISTIC (no LLM, no music/beat detection) and ALWAYS on.
It does two things, combined into one ``zoompan`` expression:

  1. PUNCHES on editorial sentence boundaries. We read the cut transcript's
     word timings, find the start time of every sentence (the first word, plus
     the start of any word that FOLLOWS a sentence-ending word), and at those
     times cycle through ``PUNCH_FACTORS`` ("punch in / out"). Punches are
     spaced at least ``MIN_PUNCH_GAP_SEC`` apart.

  2. A constant smooth DRIFT within every shot: after each punch the zoom
     ramps a further ``DRIFT_DELTA`` across that segment (resetting to the new
     punch level at the next punch), so the camera is always gently moving --
     not frozen between punches.

The cropped window is horizontally CENTERED and vertically TOP-biased
(``TOP_BIAS``) so the visible region sits in the TOP HALF of the frame for any
output format.

SHORTS additionally open on a CRASH ZOOM with a radial (speed) zoom blur: the
base layer accelerates inward over ``SHORT_CRASH_SEC`` and settles a touch
closer (``SHORT_CRASH_ZOOM``), while a radial zoom-blur streaks from the centre
-- ramping in at ~50ms, peaking ~150ms, and gone by the end of the window. The
blur is faked the classic way (no native radial-blur filter exists in ffmpeg):
the framed base is sampled at ``SHORT_BLUR_LAYERS`` progressively-larger
concentric scales and averaged, with the scale spread driven by the intensity
envelope so it's a true radial smear that vanishes (zero cost) after the crash.
Long / faceless-long get NONE of this -- their plan carries crash/blur = 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from contenido_bionico.shared.captions import clean_words

DRIFT_DELTA = 0.06            # smooth drift added WITHIN each shot: after every punch the zoom ramps +DRIFT_DELTA across that segment, then the next punch resets the level
PUNCH_FACTORS = (1.0, 1.10, 1.0, 1.13)   # cycled at successive sentence starts ("punch in / out")
TOP_BIAS = 0.12               # vertical crop anchor (0=top edge, 0.5=center). Keeps window in TOP half.
MIN_PUNCH_GAP_SEC = 1.2       # never punch more often than this
SUPERSAMPLE = 3               # render zoompan at 3x then downscale -> sub-pixel-smooth drift
_SENT_END = (".", "?", "!", "…")

# --- short-only opening crash zoom + radial (speed) zoom blur -----------------
SHORT_CRASH_SEC = 0.8         # length of the opening crash-zoom window (seconds)
SHORT_CRASH_ZOOM = 0.08       # base layer ends this much closer after the crash (persists; punch/drift continue on top)
SHORT_BLUR_MAX = 0.18         # peak extra zoom of the OUTERMOST blur layer -> radial streak length at max intensity
SHORT_BLUR_LAYERS = 12        # concentric scaled copies averaged into the radial blur (more = smoother streaks)
SHORT_BLUR_CENTER = 0.38      # vertical origin of the radial blur (0=top, 0.5=center); sits near the face in the top-biased framing


@dataclass(frozen=True)
class CameraPlan:
    duration: float
    drift_delta: float
    top_bias: float
    punches: tuple                       # tuple[tuple[float, float], ...] -> (time_sec, zoom_factor), sorted, first t == 0.0
    crash_zoom: float = 0.0             # opening crash-zoom: extra zoom that ramps in over crash_seconds and PERSISTS (shorts only; 0 disables)
    crash_seconds: float = 0.0          # length of the opening crash-zoom + radial-blur window
    blur_max: float = 0.0               # peak extra zoom of the outermost radial-blur layer (0 disables the blur)
    blur_layers: int = 0                # number of concentric copies averaged for the radial blur

    def to_dict(self) -> dict:
        return {
            "duration": self.duration,
            "drift_delta": self.drift_delta,
            "top_bias": self.top_bias,
            "punches": [[float(t), float(z)] for t, z in self.punches],
            "crash_zoom": self.crash_zoom,
            "crash_seconds": self.crash_seconds,
            "blur_max": self.blur_max,
            "blur_layers": self.blur_layers,
        }

    @staticmethod
    def from_dict(d: dict) -> "CameraPlan":
        raw_punches = d.get("punches") or []
        punches = tuple((float(row[0]), float(row[1])) for row in raw_punches)
        if not punches:
            punches = ((0.0, PUNCH_FACTORS[0]),)
        return CameraPlan(
            duration=float(d["duration"]),
            drift_delta=float(d["drift_delta"]),
            top_bias=float(d["top_bias"]),
            punches=punches,
            crash_zoom=float(d.get("crash_zoom", 0.0)),
            crash_seconds=float(d.get("crash_seconds", 0.0)),
            blur_max=float(d.get("blur_max", 0.0)),
            blur_layers=int(d.get("blur_layers", 0)),
        )


def sentence_starts(words: Sequence[dict[str, Any]], duration: float) -> list[float]:
    """Sorted start-times of editorial sentences.

    Includes the first word's start, and the start of any word that FOLLOWS a
    word whose text ends with a sentence terminator (``_SENT_END``). Results are
    clamped to ``[0, duration)``, deduplicated, and sorted. Word rows are
    normalized with ``shared.captions.clean_words`` (same contract as captions).
    """
    cleaned = clean_words(words, duration)
    if not cleaned:
        return []
    starts: set[float] = set()
    # First word always opens a sentence.
    first = cleaned[0]["start"]
    if 0.0 <= first < duration:
        starts.add(first)
    for prev, cur in zip(cleaned, cleaned[1:]):
        if prev["word"].endswith(_SENT_END):
            s = cur["start"]
            if 0.0 <= s < duration:
                starts.add(s)
    return sorted(starts)


def plan_camera(
    words: Sequence[dict[str, Any]],
    duration: float,
    fmt: str,
    *,
    top_bias: float | None = None,
) -> CameraPlan:
    """Build a deterministic CameraPlan.

    Walks the sentence starts in order, honoring ``MIN_PUNCH_GAP_SEC`` between
    consecutive punches, and assigns ``PUNCH_FACTORS`` cycled. The plan ALWAYS
    includes ``(0.0, PUNCH_FACTORS[0])`` as its first punch. With no usable
    words the plan is a single ``(0.0, 1.0)`` punch (drift-only camera).

    Shorts (``fmt == "short"``) additionally get the opening crash-zoom + radial
    zoom blur (``SHORT_CRASH_*`` / ``SHORT_BLUR_*``). Long / faceless-long get a
    plain plan (crash/blur fields stay 0).

    ``top_bias`` overrides the module ``TOP_BIAS`` stamped into the returned
    plan; ``None`` (the default) keeps the module value. The ranking format
    passes a more centered bias (~0.35) since its face-crop pre-pass already
    frames the head high in the 9:8 window.
    """
    starts = sentence_starts(words, duration)

    punches: list[tuple[float, float]] = [(0.0, PUNCH_FACTORS[0])]
    last_t = 0.0
    factor_idx = 1
    for t in starts:
        if t <= 0.0:
            # 0.0 is already the first punch.
            continue
        if t - last_t < MIN_PUNCH_GAP_SEC:
            continue
        z = PUNCH_FACTORS[factor_idx % len(PUNCH_FACTORS)]
        punches.append((float(t), float(z)))
        last_t = t
        factor_idx += 1

    is_short = fmt == "short"
    return CameraPlan(
        duration=float(duration),
        drift_delta=DRIFT_DELTA,
        top_bias=TOP_BIAS if top_bias is None else float(top_bias),
        punches=tuple(punches),
        crash_zoom=SHORT_CRASH_ZOOM if is_short else 0.0,
        crash_seconds=SHORT_CRASH_SEC if is_short else 0.0,
        blur_max=SHORT_BLUR_MAX if is_short else 0.0,
        blur_layers=SHORT_BLUR_LAYERS if is_short else 0,
    )


def _balanced_sum(terms: Sequence[str]) -> str:
    """Combine ``terms`` with ``+`` as a balanced binary tree.

    A flat ``a+b+c+...`` parses left-associatively into an AST whose depth is
    ``len(terms)``; ffmpeg's expression evaluator overflows past ~80 levels
    ("Missing ')' or too many args"). A balanced tree keeps the depth at
    ``log2(n)``, so any number of punches parses. Addition is associative, so
    the value is identical to a flat sum.
    """
    if len(terms) == 1:
        return terms[0]
    if len(terms) == 2:
        return f"({terms[0]}+{terms[1]})"
    mid = len(terms) // 2
    return f"({_balanced_sum(terms[:mid])}+{_balanced_sum(terms[mid:])})"


def _zoom_expr(
    punches: Sequence[tuple[float, float]],
    duration: float,
    drift_delta: float,
    crash_zoom: float = 0.0,
    crash_seconds: float = 0.0,
    time_var: str = "time",
) -> str:
    """Per-segment punch + drift as one zoompan ``z`` expression.

    Each punch opens a segment that runs until the next punch (the last runs to
    ``duration``). Within a segment the zoom holds the punch level and ramps a
    further ``drift_delta`` linearly across it (a slight, constant push), then
    the next punch resets to its level and drifts again.

    Built as a flat sum of mutually-exclusive segment terms, each gated by a
    ``gte(time,t_i)*lt(time,t_next)`` window indicator so exactly one term is
    live at any instant. The sum is assembled as a balanced tree (see
    ``_balanced_sum``) so the AST depth stays ``log2(n)`` and ffmpeg can parse
    it for any punch count — a long video can have 100+ punches, which a nested
    ``if(...)`` chain (depth = n) could not. Numerically and pixel-for-pixel
    identical to the old nested form for any n.

    When ``crash_zoom > 0`` the opening crash-zoom is added on TOP: an extra zoom
    that accelerates in over ``crash_seconds`` (ease-out) and then PERSISTS, so
    the short opens by rushing in and settles a touch closer while punch/drift
    carry on normally.
    """
    sorted_p = sorted(punches, key=lambda row: row[0])
    n = len(sorted_p)

    def seg(i: int) -> str:
        t_i, z_i = sorted_p[i]
        t_next = sorted_p[i + 1][0] if i + 1 < n else duration
        seg_len = t_next - t_i
        if seg_len < 0.001:
            seg_len = 0.001
        ramp = f"min(1,max(0,({time_var}-{t_i:.3f})/{seg_len:.3f}))"
        return f"({z_i:.3f}+{drift_delta:.3f}*{ramp})"

    if n == 1:
        expr = seg(0)
    else:
        # seg(0) is live from the start until the first switch at t_1; each
        # interior seg(i) is windowed to [t_i, t_{i+1}); the last runs to the
        # end. The windows tile [0, inf) with no gap/overlap, so the sum always
        # equals the active segment (never 0, which would be an invalid zoom).
        terms = [f"(lt({time_var},{sorted_p[1][0]:.3f})*{seg(0)})"]
        for i in range(1, n - 1):
            t_i = sorted_p[i][0]
            t_next = sorted_p[i + 1][0]
            terms.append(
                f"(gte({time_var},{t_i:.3f})*lt({time_var},{t_next:.3f})*{seg(i)})"
            )
        terms.append(f"(gte({time_var},{sorted_p[n - 1][0]:.3f})*{seg(n - 1)})")
        expr = _balanced_sum(terms)

    if crash_zoom > 0.0 and crash_seconds > 0.0:
        p = f"clip({time_var}/{crash_seconds:.4f},0,1)"
        push = f"(1-pow(1-{p},2.0))"   # ease-out: fast in, settles by crash_seconds, then holds
        expr = f"({expr}+{crash_zoom:.4f}*{push})"
    return expr


def _blur_env_expr(crash_seconds: float) -> str:
    """Radial-blur INTENSITY envelope as a zoompan expression in ``time``.

    Matches the crash-zoom feel: ~0 until ``~0.0625*crash_seconds`` (50ms at
    0.8s), rises to a peak at ``~0.1875*crash_seconds`` (150ms), then decays
    (pow 1.6, so it's already noticeably softer by ~300ms and faint by ~500ms)
    to exactly 0 at ``crash_seconds``. Stays 0 afterwards -> the blur fully
    disappears and the layers collapse back onto the base.
    """
    cs = crash_seconds
    t_start = 0.0625 * cs
    t_peak = 0.1875 * cs
    rise = f"clip((time-{t_start:.4f})/{(t_peak - t_start):.4f},0,1)"
    decay = f"pow(clip(({cs:.4f}-time)/{(cs - t_peak):.4f},0,1),1.6)"
    return f"({rise}*{decay})"


def source_chain(
    plan: CameraPlan,
    *,
    out_w: int,
    out_h: int,
    fps: int,
    in_label: str = "0:v",
    out_label: str = "base",
) -> str:
    """Return one ffmpeg filter-chain string for the camera base layer.

    Without an opening crash blur (long / faceless-long, or any plan with
    ``blur_layers < 2``) this is the classic single chain:

    ``[in]scale,pad,setsar,fps,zoompan,downscale[out]``

    The source is scaled/padded (keep aspect, centered, black pad) to
    ``out_w x out_h``, then ``zoompan`` is ALWAYS applied (the drift is always
    non-trivial). The zoom expression combines the nested-if punch step with a
    linear drift; the cropped window is horizontally centered and top-biased.

    With an opening crash blur (shorts), the framed base stream is then SPLIT
    into ``blur_layers`` concentric copies -- each an extra zoompan that pushes
    in by ``blur_max * (k/(K-1)) * env(time)`` about the blur centre -- and the
    copies are averaged with ``mix``. ``env(time)`` is the intensity envelope, so
    the copies fan out into a radial streak at the peak and collapse back onto
    the base (zero blur) once the window ends.
    """
    duration = plan.duration if plan.duration > 0 else 1.0
    z_expr = _zoom_expr(
        plan.punches, duration, plan.drift_delta, plan.crash_zoom, plan.crash_seconds
    )
    # SUPERSAMPLE the source SUPERSAMPLE x before zoompan, then scale back down.
    # zoompan rounds its crop window to integer pixels every frame, so a slow
    # drift on the final-size frame lands on a coarse grid (a few visible jumps).
    # Running zoompan on a 3x-larger frame puts the SAME motion on a 3x-finer
    # grid (~0.33px steps in final pixels -> ~13 micro-steps instead of ~4
    # jumps), below the visible threshold. Cost: zoompan processes 9x the pixels.
    ss = SUPERSAMPLE
    ss_w, ss_h = out_w * ss, out_h * ss
    base = (
        f"scale={ss_w}:{ss_h}:force_original_aspect_ratio=decrease,"
        f"pad={ss_w}:{ss_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps}"
    )
    zoompan = (
        f"zoompan=z='{z_expr}':d=1"
        f":x='iw/2-(iw/zoom)/2'"
        f":y='(ih-ih/zoom)*{plan.top_bias:.3f}'"
        f":s={ss_w}x{ss_h}:fps={fps}"
    )
    downscale = f"scale={out_w}:{out_h}:flags=lanczos"

    blur_on = (
        plan.blur_max > 0.0
        and plan.blur_layers >= 2
        and plan.crash_seconds > 0.0
    )
    if not blur_on:
        return f"[{in_label}]{base},{zoompan},{downscale},setsar=1,fps={fps}[{out_label}]"

    # Framed base at final resolution, then a radial zoom-blur built from K
    # concentric copies. Internal labels are prefixed `cz` to stay unique inside
    # the larger filter_complex the caller assembles.
    cam = "czcam"
    chains = [f"[{in_label}]{base},{zoompan},{downscale},setsar=1,fps={fps}[{cam}]"]
    K = int(plan.blur_layers)
    env = _blur_env_expr(plan.crash_seconds)
    splits = "".join(f"[czs{k}]" for k in range(K))
    chains.append(f"[{cam}]split={K}{splits}")
    labels: list[str] = []
    for k in range(K):
        if k == 0:
            z_layer = "1"  # anchor copy == the base, no extra zoom
        else:
            amp = plan.blur_max * (k / (K - 1))
            z_layer = f"(1+{amp:.5f}*{env})"
        zp = (
            f"zoompan=z='{z_layer}':d=1"
            f":x='iw/2-(iw/zoom)/2'"
            f":y='(ih-ih/zoom)*{SHORT_BLUR_CENTER:.3f}'"
            f":s={out_w}x{out_h}:fps={fps}"
        )
        chains.append(f"[czs{k}]{zp}[czb{k}]")
        labels.append(f"[czb{k}]")
    chains.append(f"{''.join(labels)}mix=inputs={K}[{out_label}]")
    return ";".join(chains)


# --- SHORT split-screen: TOP-half base ----------------------------------------
# The split-screen short renders the talking head into the TOP half only
# (out_w x out_h, a 9:8 region). A FIXED 9:8 window is cropped from the (9:16)
# source — vertically anchored at SPLIT_FACE_CENTER — and scaled to fill the top
# half; its SIZE is driven by the zoom expression (crash + punches + drift). The
# crop does NOT follow the speaker, so the camera only moves IN/OUT (closer /
# further + punches), never up/down — same motion as the full-frame source_chain.
SPLIT_FACE_CENTER = (0.5, 0.42)   # fixed top-half crop center (fraction of frame W/H)
SPLIT_BLUR_CENTER = 0.5           # radial-blur origin: the face sits centered in the top half


def source_chain_split_top(
    plan: CameraPlan,
    *,
    out_w: int,
    out_h: int,
    fps: int,
    in_label: str = "0:v",
    out_label: str = "toph",
) -> str:
    """Return one ffmpeg filter-chain for the SHORT split-screen TOP-half base.

    The source is normalized to 1080x1920 and supersampled, then framed in two
    fixed steps:

      1. CROP: a FIXED 9:8 window (``out_w:out_h`` aspect, full width) is cut
         from the top region, vertically anchored at ``SPLIT_FACE_CENTER`` and
         clamped to the frame. It does NOT follow the speaker.
      2. ZOOM (crash + punches + drift): ``zoompan`` zooms the 9:8 frame about
         its center and scales to ``out_w x out_h``. Input and output are both
         9:8, so there is NO aspect distortion. Because the crop is fixed and
         the zoom is centered, the camera only moves IN/OUT (closer / further +
         punches), never up/down — the same motion as ``source_chain``.

    When the plan carries a crash blur (``blur_layers >= 2``) the framed top is
    split into concentric zoom copies and averaged into a radial streak, exactly
    like ``source_chain`` but at the top-half resolution.
    """
    duration = plan.duration if plan.duration > 0 else 1.0
    ss = SUPERSAMPLE
    # Supersampled source (9:16) and the FIXED 9:8 crop window (full width).
    src_w, src_h = 1080 * ss, 1920 * ss
    win_w, win_h = out_w * ss, out_h * ss  # 9:8, full width

    z_expr = _zoom_expr(
        plan.punches, duration, plan.drift_delta, plan.crash_zoom, plan.crash_seconds
    )

    # Normalize source -> 1080x1920, supersample, then a FIXED-size crop (full
    # width, 9:8 tall) vertically anchored at SPLIT_FACE_CENTER and clamped.
    norm = (
        f"scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps},"
        f"scale={src_w}:{src_h}"
    )
    pan = (
        f"crop=w={win_w}:h={win_h}:x=(in_w-{win_w})/2"
        f":y='clip({SPLIT_FACE_CENTER[1]:.4f}*in_h-{win_h // 2},0,in_h-{win_h})'"
    )
    # Zoom about the center of the 9:8 frame, scaling to out_w x out_h.
    zoom = (
        f"zoompan=z='{z_expr}':d=1"
        f":x='iw/2-(iw/zoom)/2'"
        f":y='ih/2-(ih/zoom)/2'"
        f":s={win_w}x{win_h}:fps={fps}"
    )
    downscale = f"scale={out_w}:{out_h}:flags=lanczos"

    blur_on = (
        plan.blur_max > 0.0
        and plan.blur_layers >= 2
        and plan.crash_seconds > 0.0
    )
    if not blur_on:
        return (
            f"[{in_label}]{norm},{pan},{zoom},{downscale},setsar=1,fps={fps}[{out_label}]"
        )

    # Framed top at final resolution, then the radial zoom-blur (concentric
    # copies averaged) about the top-half center. Internal labels prefixed `czt`.
    cam = "cztcam"
    chains = [f"[{in_label}]{norm},{pan},{zoom},{downscale},setsar=1,fps={fps}[{cam}]"]
    K = int(plan.blur_layers)
    env = _blur_env_expr(plan.crash_seconds)
    splits = "".join(f"[czts{k}]" for k in range(K))
    chains.append(f"[{cam}]split={K}{splits}")
    labels: list[str] = []
    for k in range(K):
        if k == 0:
            z_layer = "1"
        else:
            amp = plan.blur_max * (k / (K - 1))
            z_layer = f"(1+{amp:.5f}*{env})"
        zp = (
            f"zoompan=z='{z_layer}':d=1"
            f":x='iw/2-(iw/zoom)/2'"
            f":y='(ih-ih/zoom)*{SPLIT_BLUR_CENTER:.3f}'"
            f":s={out_w}x{out_h}:fps={fps}"
        )
        chains.append(f"[czts{k}]{zp}[cztb{k}]")
        labels.append(f"[cztb{k}]")
    chains.append(f"{''.join(labels)}mix=inputs={K}[{out_label}]")
    return ";".join(chains)
