/**
 * posterKit — shared building blocks for the poster formats.
 *
 * The defining move: the HEADLINE is the hero. GiantStack sizes each line so it
 * fills ~96% of the canvas width, stacked to occupy most of the height; the
 * subject cutout is composited ON TOP (opaque, no echo) so the type reads as a
 * frame-filling backdrop the person stands in front of.
 *
 * Only Poppins is embedded in the render (the font-freeze constraint), so
 * "condensed" magazine type is faked with a horizontal scaleX squeeze + tight
 * negative tracking. Latin-1 text only.
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { fitText } from "@remotion/layout-utils";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";
import { posterEnter, PosterAnim } from "./motion";

loadBrandFonts();

export const POSTER_W = 1080;
export const POSTER_H = 1350;
export const POSTER_FONT = `'${BRAND_FONT_FAMILY}', sans-serif`;

export type Palette = {
  bg: string;
  ink: string;
  paper: string;
  accent: string;
  accentSoft: string;
};

/** Largest Poppins size (at `weight`) that fits `text` on one line within
 * `width`, after accounting for a horizontal `condense` squeeze. */
export const fitToWidth = (
  text: string,
  width: number,
  weight: number | string = "900",
  condense = 1,
  letterSpacing = "-0.02em",
): number => {
  const { fontSize } = fitText({
    text: text || " ",
    withinWidth: width / condense,
    fontFamily: BRAND_FONT_FAMILY,
    fontWeight: weight,
    letterSpacing,
  });
  return fontSize;
};

// --- light-text legibility shadow --------------------------------------------
// LIGHT-colored poster text (white / cream / near-white) carries a tight, high-
// opacity dark drop shadow so it stays crisp over ANY background — a dark solid,
// a photo plate, or a bright spot in a photo — in every mode (solid + foto) and
// medium (still png + mp4). Dark text (e.g. ink on a cream page) gets none: the
// shadow is keyed on the text color's luminance, so it self-selects.
export const LIGHT_TEXT_SHADOW = "0 2px 4px rgba(0, 0, 0, 0.8)";

/** sRGB relative luminance (0..1) of a #RRGGBB color. Non-hex colors (named /
 * rgb()) are treated as light so they still get the lift. */
const relLuminance = (color: string): number => {
  const m = /^#?([0-9a-fA-F]{6})$/.exec((color ?? "").trim());
  if (!m) return 1;
  const n = parseInt(m[1], 16);
  const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => v / 255);
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};

/** True when a text color is light enough to warrant the dark drop shadow. The
 * 0.6 cut keeps white/cream in and leaves mid-tone accents (e.g. orange) out. */
export const isLightText = (color: string): boolean => relLuminance(color) >= 0.6;

/** The 80%-opacity, low-diffusion drop shadow for a LIGHT text `color`, or
 * `undefined` for dark text (which needs no lift). `size` scales the shadow so a
 * giant headline gets a proportional — still tight — lift while small labels use
 * the fixed low-diffusion value. */
export const lightTextShadow = (color: string, size?: number): string | undefined => {
  if (!isLightText(color)) return undefined;
  if (size && size > 80) {
    const off = Math.max(2, Math.round(size * 0.02));
    const blur = Math.max(4, Math.round(size * 0.045));
    return `0 ${off}px ${blur}px rgba(0, 0, 0, 0.8)`;
  }
  return LIGHT_TEXT_SHADOW;
};

// Low-importance words that must never sit ALONE on a headline line — they get
// merged into a neighbor so every line carries real weight and fills the width.
const FUNCTION_WORDS = new Set([
  "el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "al", "del",
  "de", "y", "o", "u", "e", "en", "a", "que", "su", "sus", "mi", "mis", "tu",
  "tus", "yo", "se", "le", "les", "es", "no", "ni", "con", "por", "para", "si",
  "ya", "me", "te", "nos", "the", "a", "an", "of", "to", "in", "on", "and", "or",
]);

/** Smallest max-line char length (words only, no spaces) for which the words
 * greedily pack into at most `k` lines. Binary-searched so the resulting lines
 * are as BALANCED (equal width) as possible for that line count. */
const minMaxCap = (lens: number[], k: number): number => {
  const needed = (cap: number): number => {
    let lines = 1;
    let cur = 0;
    for (const l of lens) {
      if (cur > 0 && cur + l > cap) {
        lines += 1;
        cur = l;
      } else {
        cur += l;
      }
    }
    return lines;
  };
  let lo = Math.max(...lens);
  let hi = lens.reduce((s, l) => s + l, 0);
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (needed(mid) <= k) hi = mid;
    else lo = mid + 1;
  }
  return lo;
};

const mergeLoneFunctionWords = (lines: string[]): string[] => {
  if (lines.length <= 1) return lines;
  const out = [...lines];
  for (let i = 0; i < out.length; i++) {
    const w = out[i].trim();
    if (out.length <= 1) break;
    if (w.includes(" ")) continue; // not a lone word
    if (!FUNCTION_WORDS.has(w.toLowerCase())) continue; // lone but important -> keep
    // Merge the lone function word into the shorter neighbor.
    const prevLen = i > 0 ? out[i - 1].length : Infinity;
    const nextLen = i < out.length - 1 ? out[i + 1].length : Infinity;
    if (nextLen <= prevLen && i < out.length - 1) {
      out[i + 1] = `${w} ${out[i + 1]}`;
      out.splice(i, 1);
      i -= 1;
    } else if (i > 0) {
      out[i - 1] = `${out[i - 1]} ${w}`;
      out.splice(i, 1);
      i -= 1;
    }
  }
  return out;
};

/** Re-flow a headline into BALANCED lines that each ~fill the canvas width, so
 * type is as big as the empty space allows and no short/function word is ever
 * stranded alone on a line. Word-width is approximated by character count
 * (uppercase Poppins is near-uniform), which is enough to balance line widths.
 * `targetChars` steers the line count: ~8 favors 2-3 punchy lines on a poster. */
export const balanceHeadline = (
  text: string,
  maxLines = 4,
  targetChars = 8,
): string[] => {
  const words = (text ?? "").trim().split(/\s+/).filter(Boolean);
  if (words.length <= 1) return words.length ? words : [" "];
  const lens = words.map((w) => w.length);
  const total = lens.reduce((s, l) => s + l, 0);
  const k = Math.min(maxLines, words.length, Math.max(1, Math.round(total / targetChars)));
  const cap = minMaxCap(lens, k);
  const lines: string[] = [];
  let cur: string[] = [];
  let curLen = 0;
  for (const w of words) {
    if (cur.length > 0 && curLen + w.length > cap) {
      lines.push(cur.join(" "));
      cur = [w];
      curLen = w.length;
    } else {
      cur.push(w);
      curLen += w.length;
    }
  }
  if (cur.length) lines.push(cur.join(" "));
  return mergeLoneFunctionWords(lines);
};

export type GiantStackProps = {
  lines: string[];
  color: string;
  /** Re-flow `lines` into balanced, width-filling lines (default true). The
   * incoming `lines` are treated as the source words, regardless of how they
   * were split upstream — the layout controls WHERE the headline sits, this
   * controls how it BREAKS. */
  rebalance?: boolean;
  /** Max lines when rebalancing (default 4). */
  maxLines?: number;
  /** Line-count steer when rebalancing (default 8 chars/line). */
  targetChars?: number;
  /** Fraction of POSTER_W the widest line fills (default 0.96). */
  widthPct?: number;
  /** Horizontal squeeze to fake condensed type (default 0.82 — narrower = more condensed). */
  condense?: number;
  weight?: number | string;
  lineHeight?: number;
  letterSpacing?: string;
  /** Outline instead of filled fill (Leotude-style). */
  outline?: boolean;
  outlineWidth?: number;
  opacity?: number;
  align?: "left" | "center" | "right";
  style?: React.CSSProperties;
};

/** A block of huge stacked lines, each sized so the WIDEST fills widthPct of the
 * canvas — one shared size so the block reads as one headline. */
export const GiantStack: React.FC<GiantStackProps> = ({
  lines,
  color,
  rebalance = true,
  maxLines = 4,
  targetChars = 8,
  widthPct = 0.96,
  condense = 0.82,
  weight = "900",
  lineHeight = 0.9,
  letterSpacing = "-0.03em",
  outline = false,
  outlineWidth = 3,
  opacity = 1,
  align = "left",
  style,
}) => {
  const source = (lines ?? []).map((l) => (l ?? "").trim()).filter(Boolean);
  const reflowed = rebalance
    ? balanceHeadline(source.join(" "), maxLines, targetChars)
    : source;
  const clean = reflowed.map((l) => l.toUpperCase()).filter(Boolean);
  const safe = clean.length ? clean : [" "];
  const target = POSTER_W * widthPct;
  const size = Math.min(
    ...safe.map((l) => fitToWidth(l, target, weight, condense, letterSpacing)),
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: align === "center" ? "center" : align === "right" ? "flex-end" : "flex-start", ...style }}>
      {safe.map((line, i) => (
        <div
          key={i}
          style={{
            fontFamily: POSTER_FONT,
            fontWeight: weight,
            fontSize: size,
            lineHeight,
            letterSpacing,
            whiteSpace: "nowrap",
            transform: `scaleX(${condense})`,
            transformOrigin: align === "right" ? "right center" : align === "center" ? "center" : "left center",
            color: outline ? "transparent" : color,
            opacity,
            WebkitTextStroke: outline ? `${outlineWidth}px ${color}` : undefined,
            textShadow: lightTextShadow(color, size),
          }}
        >
          {line}
        </div>
      ))}
    </div>
  );
};

/** The subject cutout. Defaults to head-anchored cover inside a box that starts
 * `top` px down (leaving a clear strip for an eyebrow) and runs to the bottom —
 * so the head is always visible and the crop takes the legs, not the face. */
export const Subject: React.FC<{
  src?: string;
  objectFit?: "cover" | "contain";
  objectPosition?: string;
  top?: number;
  style?: React.CSSProperties;
}> = ({ src, objectFit = "cover", objectPosition = "top center", top = 0, style }) => {
  if (!src) return null;
  return (
    <Img
      src={staticFile(src)}
      style={{
        position: "absolute",
        top,
        left: 0,
        width: POSTER_W,
        height: POSTER_H - top,
        objectFit,
        objectPosition,
        ...style,
      }}
    />
  );
};

/** The whole poster stack, shared by every layout. Realizes two invariants:
 *  - background: a designed solid/gradient (`bgNode`, solid mode) OR the real
 *    photo plate (`bgSrc`, foto mode) — never an invented photo.
 *  - layering: `behind` renders UNDER the subject (top-of-frame text tucks
 *    behind the face) and `front` renders OVER it (bottom-of-frame text covers
 *    the body). In foto mode the plate and cutout share identical cover geometry
 *    so the person lands exactly where they are in the plate.
 * The layout only decides WHICH elements are `behind` vs `front` (by their
 * vertical position) and what the solid `bgNode` looks like. */
export const FOTO_OBJECT_POSITION = "top center";

export const PosterFrame: React.FC<{
  mode: "solid" | "foto";
  bgColor: string;
  bgNode?: React.ReactNode;   // solid-mode designed background
  bgSrc?: string;             // foto-mode photo plate
  subjectSrc?: string;        // foreground cutout (both modes)
  subjectTop?: number;        // solid-mode subject top offset (foto = 0, to align with the plate)
  behind?: React.ReactNode;
  front?: React.ReactNode;
  /** This layout's signature entrance when rendered as video (default slide).
   * Each PosterLayout passes a distinct one so the four styles read as four
   * different moves. */
  anim?: PosterAnim;
}> = ({ mode, bgColor, bgNode, bgSrc, subjectSrc, subjectTop = 0, behind, front, anim = "slide" }) => {
  const foto = mode === "foto";
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  // Auto-animate when rendered as VIDEO (multi-frame): the text groups enter with
  // this layout's signature move (`anim`), behind before front. At
  // durationInFrames === 1 (the static poster stills) nothing moves, so the
  // images are byte-for-byte unchanged.
  const animate = durationInFrames > 1;
  const plateStyle: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    width: POSTER_W,
    height: POSTER_H,
    objectFit: "cover",
    objectPosition: FOTO_OBJECT_POSITION,
  };
  // Explicit z-order so the entrance transforms never reorder the layering.
  const wrap = (node: React.ReactNode, from: "top" | "bottom", startSec: number, z: number) =>
    animate ? (
      <div
        style={{
          position: "absolute",
          inset: 0,
          zIndex: z,
          ...posterEnter(frame, fps, anim, { from, startSec, durSec: 0.7, travelPx: 130 }),
        }}
      >
        {node}
      </div>
    ) : (
      <div style={{ position: "absolute", inset: 0, zIndex: z }}>{node}</div>
    );
  return (
    <AbsoluteFill style={{ backgroundColor: bgColor }}>
      {foto ? (bgSrc ? <Img src={staticFile(bgSrc)} style={plateStyle} /> : null) : bgNode}
      {wrap(behind, "top", 0.15, 0)}
      {foto ? (
        subjectSrc ? <Img src={staticFile(subjectSrc)} style={{ ...plateStyle, zIndex: 1 }} /> : null
      ) : (
        <Subject src={subjectSrc} top={subjectTop} objectPosition={FOTO_OBJECT_POSITION} style={{ zIndex: 1 }} />
      )}
      {wrap(front, "bottom", 0.45, 2)}
    </AbsoluteFill>
  );
};

/** Decorative barcode (pure graphic — carries no data). */
export const Barcode: React.FC<{ color: string; w?: number; h?: number; style?: React.CSSProperties }> = ({
  color,
  w = 120,
  h = 46,
  style,
}) => {
  const widths = [3, 1, 2, 4, 1, 3, 1, 1, 2, 5, 1, 2, 3, 1, 4, 1, 2, 1, 3, 2, 1, 4, 1, 2];
  let x = 0;
  const bars: React.ReactNode[] = [];
  widths.forEach((bw, i) => {
    if (i % 2 === 0) bars.push(<rect key={i} x={x} y={0} width={bw} height={h} fill={color} />);
    x += bw + 1;
  });
  return (
    <svg width={w} height={h} viewBox={`0 0 ${x} ${h}`} preserveAspectRatio="none" style={style}>
      {bars}
    </svg>
  );
};

/** Decorative registration crosshair. */
export const Crosshair: React.FC<{ color: string; size?: number; style?: React.CSSProperties }> = ({
  color,
  size = 34,
  style,
}) => (
  <svg width={size} height={size} viewBox="0 0 34 34" style={style}>
    <line x1="17" y1="2" x2="17" y2="32" stroke={color} strokeWidth="2" />
    <line x1="2" y1="17" x2="32" y2="17" stroke={color} strokeWidth="2" />
  </svg>
);

/** A row of small four-point stars (KAREEM-style divider). */
export const StarRow: React.FC<{ color: string; count?: number; size?: number; gap?: number }> = ({
  color,
  count = 3,
  size = 18,
  gap = 26,
}) => (
  <div style={{ display: "flex", gap, alignItems: "center" }}>
    {Array.from({ length: count }).map((_, i) => (
      <svg key={i} width={size} height={size} viewBox="0 0 24 24">
        <path d="M12 0 L14 10 L24 12 L14 14 L12 24 L10 14 L0 12 L10 10 Z" fill={color} />
      </svg>
    ))}
  </div>
);

/** Small spaced-caps label (eyebrow / masthead / footer text). */
export const Micro: React.FC<{
  children: React.ReactNode;
  color: string;
  size?: number;
  tracking?: string;
  weight?: number | string;
  style?: React.CSSProperties;
}> = ({ children, color, size = 22, tracking = "0.24em", weight = 600, style }) => (
  <div
    style={{
      fontFamily: POSTER_FONT,
      fontWeight: weight,
      fontSize: size,
      letterSpacing: tracking,
      color,
      textTransform: "uppercase",
      textShadow: lightTextShadow(color, size),
      ...style,
    }}
  >
    {children}
  </div>
);
