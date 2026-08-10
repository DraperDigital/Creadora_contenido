/**
 * Operator motion language (V6.1) — the ONE transition system for every
 * animated format comp. Do not roll per-comp variants; import from here.
 *
 * Spec:
 * - Elements ENTER sliding from their NEAREST layout edge (top/bottom/left/
 *   right) with a simultaneous blur + fade-in. The travel is quick at first
 *   and decelerates into the settle (expo ease-out), 500–800 ms total.
 * - Elements EXIT with a fade-out + blur (no slide).
 * - Backgrounds are FULL-BLEED and never slide; only elements move.
 * - Word-sync formats (karaoke) keep their voice-driven timing: use the same
 *   blur+fade character but cap durations to the sync gaps instead of the
 *   500–800 ms element timing.
 */
import type {CSSProperties} from 'react';

export type EnterFrom = 'top' | 'bottom' | 'left' | 'right';

export const ENTER_MIN_SEC = 0.5;
export const ENTER_MAX_SEC = 0.8;
export const DEFAULT_ENTER_SEC = 0.65;
export const DEFAULT_EXIT_SEC = 0.35;
export const DEFAULT_TRAVEL_PX = 140;
export const DEFAULT_BLUR_PX = 18;

/** Fast start, decelerating settle — the "quick then slows down" curve. */
export const easeOutExpo = (t: number): number =>
  t >= 1 ? 1 : t <= 0 ? 0 : 1 - Math.pow(2, -10 * t);

/** Entrance progress 0..1 for an element whose entrance starts at `startSec`. */
export const enterProgress = (
  frame: number,
  fps: number,
  startSec: number,
  durSec: number = DEFAULT_ENTER_SEC,
): number => {
  const dur = Math.min(Math.max(durSec, ENTER_MIN_SEC), ENTER_MAX_SEC);
  return easeOutExpo((frame / fps - startSec) / dur);
};

const offsetFor = (from: EnterFrom, off: number): [number, number] => {
  switch (from) {
    case 'left':
      return [-off, 0];
    case 'right':
      return [off, 0];
    case 'top':
      return [0, -off];
    default:
      return [0, off];
  }
};

export interface EnterOpts {
  /** The layout edge this element is closest to — it slides in from there. */
  from: EnterFrom;
  /** When (in seconds on the comp timeline) the entrance begins. */
  startSec?: number;
  /** Entrance length; clamped to the 0.5–0.8 s spec window. */
  durSec?: number;
  travelPx?: number;
  blurPx?: number;
}

/**
 * Slide-from-nearest-edge + blur + fade-in entrance.
 * Spread the result into the element's style: `style={{...enterStyle(...)}}`.
 */
export const enterStyle = (
  frame: number,
  fps: number,
  {from, startSec = 0, durSec, travelPx = DEFAULT_TRAVEL_PX, blurPx = DEFAULT_BLUR_PX}: EnterOpts,
): CSSProperties => {
  const p = enterProgress(frame, fps, startSec, durSec);
  const [x, y] = offsetFor(from, (1 - p) * travelPx);
  return {
    opacity: p,
    transform: `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`,
    filter: p >= 1 ? undefined : `blur(${((1 - p) * blurPx).toFixed(2)}px)`,
  };
};

export interface ExitOpts {
  /** When (in seconds on the comp timeline) the exit begins. */
  startSec: number;
  durSec?: number;
  blurPx?: number;
}

/** Fade-out + blur exit (no slide). Progress uses the same decelerating curve. */
export const exitStyle = (
  frame: number,
  fps: number,
  {startSec, durSec = DEFAULT_EXIT_SEC, blurPx = DEFAULT_BLUR_PX}: ExitOpts,
): CSSProperties => {
  const p = easeOutExpo((frame / fps - startSec) / Math.max(durSec, 0.01));
  return {
    opacity: 1 - p,
    filter: p <= 0 ? undefined : `blur(${(p * blurPx).toFixed(2)}px)`,
  };
};

/**
 * Combined lifecycle for an element visible on [startSec, endSec): enters at
 * startSec per the entrance spec, exits (fade+blur) finishing by endSec.
 */
export const elementStyle = (
  frame: number,
  fps: number,
  opts: EnterOpts & {endSec?: number; exitSec?: number},
): CSSProperties => {
  const {endSec, exitSec = DEFAULT_EXIT_SEC, ...enter} = opts;
  if (endSec !== undefined && frame / fps >= endSec - exitSec) {
    return exitStyle(frame, fps, {startSec: endSec - exitSec, durSec: exitSec, blurPx: enter.blurPx});
  }
  return enterStyle(frame, fps, enter);
};

/**
 * Per-layout SIGNATURE entrances for the animated posters. Each poster STYLE
 * animates its text groups in differently, so the four layouts read as four
 * distinct moves — all still within the operator motion character (expo
 * ease-out, blur + fade, 0.5–0.8 s). This is the ONE place the variants live;
 * layouts pick one by name, they do not roll their own.
 *   - slide: nearest-edge translate (the default element move)
 *   - pop:   scale-up from 0.85, centered (no translate)
 *   - wipe:  directional clip-path reveal along the entrance axis
 *   - rise:  a unified upward float (ignores `from`), longer travel
 * `from` is the group's nearest layout edge (used by slide + wipe).
 */
export type PosterAnim = 'slide' | 'pop' | 'wipe' | 'rise';

export const posterEnter = (
  frame: number,
  fps: number,
  variant: PosterAnim,
  {from, startSec = 0, durSec, travelPx = DEFAULT_TRAVEL_PX, blurPx = DEFAULT_BLUR_PX}: EnterOpts,
): CSSProperties => {
  const p = enterProgress(frame, fps, startSec, durSec);
  const blur = p >= 1 ? undefined : `blur(${((1 - p) * blurPx).toFixed(2)}px)`;
  if (variant === 'pop') {
    const s = 0.85 + 0.15 * p;
    return {opacity: p, transform: `scale(${s.toFixed(4)})`, transformOrigin: 'center center', filter: blur};
  }
  if (variant === 'rise') {
    const y = (1 - p) * (travelPx + 40);
    return {opacity: p, transform: `translateY(${y.toFixed(2)}px)`, filter: blur};
  }
  if (variant === 'wipe') {
    const c = ((1 - p) * 100).toFixed(2);
    const clip =
      from === 'bottom' ? `inset(${c}% 0 0 0)`
      : from === 'left' ? `inset(0 ${c}% 0 0)`
      : from === 'right' ? `inset(0 0 0 ${c}%)`
      : `inset(0 0 ${c}% 0)`; // top (default)
    return {opacity: Math.min(1, p * 1.6), clipPath: clip, filter: blur};
  }
  // slide (default): nearest-edge translate + blur + fade.
  const [x, y] = offsetFor(from, (1 - p) * travelPx);
  return {opacity: p, transform: `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`, filter: blur};
};
