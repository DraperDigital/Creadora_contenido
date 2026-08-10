/**
 * Captions overlay composition (shared across short + long + faceless).
 *
 * Single-typography, sentence-by-sentence captions, rendered as a transparent
 * alpha overlay: every word renders in the bundled 'Poppins' face (with the
 * brand font from props.brand.font as the fallback stack), white with a HARD
 * black drop shadow for legibility over any footage.
 *
 * Reveal: each cue is a whole 5-7 word sentence that appears AS ONE BLOCK,
 * fading in and sliding up over ~250ms at the cue's start, then holding for the
 * full cue so it stays readable (no word-by-word flashing).
 *
 * placement "center" (shorts) sits the block slightly below center; "bottom"
 * (longs) pins it to the lower third.
 *
 * RANKING (tier-board) shorts have no dedicated "seam" placement; they reuse
 * the split mechanism below. Passing placement="center" with
 * split={start: 0, end: durationSec, lineYRatio: 0.5046} pins the block's
 * BOTTOM edge at y=940 of 1920 (20px above the tier-board top at y=960) for
 * the full video — equivalent to the archived ranking edition's
 * placement="seam" (same entrance timing/easing; the block anchors by its own
 * bottom edge in both). lineYRatio = 940/1920 + 0.015 compensates the +1.5%
 * offset the split branch applies below.
 */
import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadBrandFonts } from "./lib/fonts";
import type { CaptionsProps } from "./lib/types";

// The captions composition renders standalone (its own isolated entrypoint),
// so this module registers the brand fonts itself.
loadBrandFonts();

const STANDARD_COLOR = "#ffffff";
// Hard drop shadow: 2x bigger + 2x darker (stacked twice) — readable over any bg.
const CAPTION_SHADOW =
  "0 8px 16px rgba(0,0,0,0.8), 0 8px 16px rgba(0,0,0,0.8)";
// Default size; short overrides via props.fontPx (smaller, so the upstream
// char-capped cue fits the single never-wrapping row — see rowStyle below).
const CAPTION_FONT_PX = 88;
const SAFE_WIDTH_RATIO = 0.9; // 5% margin each side -> never clips the frame edges
// 900 = Poppins Black, the loaded weight closest to the previous 800 styling.
const STANDARD_WEIGHT = 900;
// Committed caption face: bundled 'Poppins' first; the brand font from props
// stays in the stack as the fallback face.
const captionFontStack = (brandFont: string): string =>
  `'Poppins', ${brandFont}`;
// Whole-sentence entrance: fade in + slide up together, over this span. ~250ms.
const ENTRANCE_SEC = 0.25;
const SLIDE_RATIO = 0.4; // slide distance as a fraction of font size

export const Captions: React.FC<CaptionsProps> = (props) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const time = frame / fps;
  const activeCue =
    props.cues.find((cue) => time >= cue.start && time < cue.end) ?? null;

  const containerStyle: React.CSSProperties = {
    background: "transparent",
    textAlign: "center",
    fontFamily: captionFontStack(props.brand.font),
  };
  const safeMarginPct = `${((1 - SAFE_WIDTH_RATIO) / 2) * 100}%`;

  if (!activeCue) {
    return <AbsoluteFill style={containerStyle} />;
  }

  const isBottom = props.placement === "bottom";
  const fontSize = props.fontPx ?? CAPTION_FONT_PX;
  const entranceFrames = Math.max(1, Math.round(ENTRANCE_SEC * fps));

  // Whole-sentence entrance: fade in + slide up as one block.
  const cueStartFrame = activeCue.start * fps;
  const p = interpolate(
    frame,
    [cueStartFrame, cueStartFrame + entranceFrames],
    [0, 1],
    {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: Easing.out(Easing.cubic),
    },
  );
  const slide = fontSize * SLIDE_RATIO * (1 - p);

  // SHORT split-screen: when a split window is active and this cue falls inside
  // it, the (center-placed) block moves up to sit just ABOVE the divider line in
  // the top (talking-head) half. The block's BOTTOM edge anchors at ~1.5% above
  // lineYRatio of the frame height. Otherwise placement is byte-for-byte the
  // current behavior (center top:60% for shorts, bottom for longs).
  const splitActive =
    !isBottom &&
    props.split != null &&
    time >= props.split.start &&
    time < props.split.end;

  // Compose the slide with the vertical-centering transform. The classic center
  // placement is anchored at top:60% and pulled up by half its height; the split
  // placement anchors by the block's own bottom edge, so no -50% pull is needed.
  const baseTranslateY = isBottom || splitActive ? "0px" : "-50%";

  const blockStyle: React.CSSProperties = {
    position: "absolute",
    left: safeMarginPct,
    right: safeMarginPct,
    opacity: p,
    transform: `translateY(calc(${baseTranslateY} + ${slide}px))`,
    ...(isBottom
      ? { bottom: "8%" }
      : splitActive
        ? { bottom: `${(1 - props.split!.lineYRatio) * 100 + 1.5}%` }
        : { top: "60%" }),
  };

  // Exactly ONE line, always — the cue is chunked upstream to a fixed character
  // cap that fits one line at this size, so it never wraps.
  const rowStyle: React.CSSProperties = {
    display: "flex",
    flexWrap: "nowrap",
    justifyContent: "center",
    alignItems: "baseline",
    columnGap: `${fontSize * 0.26}px`,
    width: "100%",
    lineHeight: 1.1,
  };

  const wordStyle: React.CSSProperties = {
    fontFamily: captionFontStack(props.brand.font),
    fontWeight: STANDARD_WEIGHT,
    fontSize,
    // Per-run override (e.g. "cambia el color de los subtítulos a amarillo");
    // absent -> the committed white default.
    color: props.color ?? STANDARD_COLOR,
    textShadow: CAPTION_SHADOW,
    whiteSpace: "pre",
  };

  return (
    <AbsoluteFill style={containerStyle}>
      <div style={blockStyle}>
        <div style={rowStyle}>
          <span style={wordStyle}>{activeCue.text}</span>
        </div>
      </div>
    </AbsoluteFill>
  );
};

export default Captions;
