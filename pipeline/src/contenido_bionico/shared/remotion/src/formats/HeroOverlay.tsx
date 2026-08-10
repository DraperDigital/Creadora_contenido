/**
 * HeroOverlay — hero phrases rendered as an alpha overlay, one at a time.
 *
 * Used by two video treatments (rendered by the Python producer at the matching
 * canvas size), with two very different zone behaviors:
 *
 *   - zone="full"    1080x1920, composited over the camera talking-head base
 *                    (video_herotext). CUTAWAY CARDS: during each phrase span
 *                    the WHOLE frame becomes an OPAQUE solid brand-color card
 *                    (palette.bg) — the video hard-cuts away from the talking
 *                    head — with the phrase in giant type; between spans the
 *                    frame is fully transparent and the camera base shows. The
 *                    text block is kept entirely above y=1536 (platform-UI safe
 *                    zone).
 *   - zone="bottom"  1080x960, composited over a solid brand-color band in the
 *                    lower half of a split screen (video_splitscreen). Keeps the
 *                    classic band look: transparent between phrases, text +
 *                    accent bar contrasting palette.bg.
 *
 * Motion follows the operator motion language (`./motion`): the phrase TEXT is
 * the element — it slides in from its nearest edge (bottom, for both zones'
 * centered/bottom-band layouts) with blur + fade on the expo ease-out, and
 * exits with fade + blur (no slide). The card BACKGROUND is full-bleed and
 * never slides: it appears/disappears with a quick fade (the "hard cut"
 * softened over ~5 frames). Poppins loads via the bundled data-URL @font-face
 * only (loadBrandFonts) — no network, no delayRender.
 */
import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { fitText } from "@remotion/layout-utils";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";
import { elementStyle } from "./motion";

loadBrandFonts();

type Phrase = { text: string; start: number; end: number };
type Palette = {
  bg: string;
  ink: string;
  paper: string;
  accent: string;
  accentSoft: string;
};
type HeroOverlayProps = {
  phrases: Phrase[];
  zone: "full" | "bottom";
  durationSec: number;
  palette: Palette;
};

const FONT_STACK = `'${BRAND_FONT_FAMILY}', sans-serif`;
const FONT_WEIGHT = 900;
const LETTER_SPACING = "-0.01em";
const MAX_PX_CARD = 150;
const MAX_PX_BOTTOM = 110;
// Global forbidden zone: keep full-mode text entirely above y=1536 (bottom 20%
// is reserved for the platform UI). Mirrors lib/config CONTENT_BOTTOM_Y.
const SAFE_BOTTOM_Y = 1536;
const MAX_WORDS_PER_LINE = 3; // cutaway statements (2-10 words) wrap into big lines

// Card background: quick fade (backgrounds never slide — motion spec).
const CARD_IN_SEC = 0.15;
const CARD_OUT_SEC = 0.2;
// Phrase text: the element. Enter at the quick end of the 0.5-0.8s spec window
// for impact; exit fade+blur finishing just before the cut back to the head.
const TEXT_ENTER_SEC = 0.5;
const TEXT_EXIT_SEC = 0.3;
const TEXT_LEAD_OUT_SEC = 0.08;
// Bottom band: shorter exit so brief dense phrases keep some hold time.
const BOTTOM_EXIT_SEC = 0.25;

// Relative luminance of a #RRGGBB hex, for picking a contrasting text color.
const hexLum = (hex: string): number => {
  const m = hex.replace("#", "");
  if (m.length < 6) return 0;
  const r = parseInt(m.slice(0, 2), 16) / 255;
  const g = parseInt(m.slice(2, 4), 16) / 255;
  const b = parseInt(m.slice(4, 6), 16) / 255;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

/** Balanced line split: <=MAX_WORDS_PER_LINE words per line, even lengths. */
const splitLines = (text: string): string[] => {
  const words = text.split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const nLines = Math.max(1, Math.ceil(words.length / MAX_WORDS_PER_LINE));
  const base = Math.floor(words.length / nLines);
  const extra = words.length % nLines;
  const lines: string[] = [];
  let i = 0;
  for (let l = 0; l < nLines; l++) {
    const take = base + (l < extra ? 1 : 0);
    lines.push(words.slice(i, i + take).join(" "));
    i += take;
  }
  return lines;
};

export const HeroOverlay: React.FC<HeroOverlayProps> = ({
  phrases,
  zone,
  palette,
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const time = frame / fps;

  const onBrand = hexLum(palette.bg) < 0.5 ? palette.paper : palette.ink;

  // ---------------------------------------------------------------- bottom ---
  // Split-screen lower band: CONSTANT animation. The hero phrases are cycled
  // (looped) so a big bold phrase is ALWAYS on the band — it slides in from the
  // bottom edge with blur+fade, holds, then exits fade+blur, immediately
  // followed by the next; never a static gap. A persistent accent bar keeps
  // subtle motion even during a hold. The cycle is derived from the running
  // time, so it fills the whole clip regardless of length.
  if (zone !== "full") {
    const list = (phrases ?? []).map((p) => (p.text ?? "").trim()).filter(Boolean);
    const withinWidth = width * 0.9;
    // Persistent motion: the accent bar's width breathes on a slow sine.
    const pulse = 0.3 + 0.14 * (0.5 + 0.5 * Math.sin(time * 1.7));
    let lines: string[] = [];
    let motion: React.CSSProperties = { opacity: 0 };
    if (list.length > 0) {
      const SLOT = 2.6; // seconds per cycled phrase (enter + hold + exit)
      const slot = Math.floor(time / SLOT);
      const idx = ((slot % list.length) + list.length) % list.length;
      lines = splitLines(list[idx].toUpperCase());
      motion = elementStyle(frame, fps, {
        from: "bottom",
        startSec: slot * SLOT,
        durSec: TEXT_ENTER_SEC,
        endSec: (slot + 1) * SLOT,
        exitSec: BOTTOM_EXIT_SEC,
      });
    }
    const fitted = lines.length
      ? Math.min(
          ...lines.map(
            (line) =>
              fitText({
                text: line,
                withinWidth,
                fontFamily: FONT_STACK,
                fontWeight: FONT_WEIGHT,
                letterSpacing: LETTER_SPACING,
                textTransform: "uppercase",
              }).fontSize,
          ),
        )
      : MAX_PX_BOTTOM;
    const fontSize = Math.min(MAX_PX_BOTTOM, fitted);
    return (
      <AbsoluteFill style={{ background: "transparent" }}>
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: fontSize * 0.14,
              maxWidth: withinWidth,
              ...motion,
            }}
          >
            {lines.map((line, i) => (
              <span
                key={i}
                style={{
                  fontFamily: FONT_STACK,
                  fontWeight: FONT_WEIGHT,
                  fontSize,
                  lineHeight: 1.02,
                  letterSpacing: LETTER_SPACING,
                  color: onBrand,
                  textTransform: "uppercase",
                  whiteSpace: "nowrap",
                  textAlign: "center",
                }}
              >
                {line}
              </span>
            ))}
          </div>
        </div>
        {/* Persistent accent bar: always in motion (width breathes) so the band
            is never fully still, even between cycled phrases. */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            bottom: height * 0.16,
            transform: "translateX(-50%)",
            width: `${(pulse * 100).toFixed(1)}%`,
            height: Math.max(6, fontSize * 0.08),
            background: palette.accent,
            borderRadius: 999,
          }}
        />
      </AbsoluteFill>
    );
  }

  // ------------------------------------------------------------------ full ---
  // Cutaway card: opaque full-bleed brand color while the phrase is being said.
  const active = (phrases ?? []).find((p) => time >= p.start && time < p.end) ?? null;
  if (!active) {
    return <AbsoluteFill style={{ background: "transparent" }} />;
  }
  const startFrame = active.start * fps;
  const endFrame = active.end * fps;
  const cardOpacity = Math.min(
    interpolate(frame, [startFrame, startFrame + CARD_IN_SEC * fps], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
    interpolate(frame, [endFrame - CARD_OUT_SEC * fps, endFrame], [1, 0], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
  );

  const lines = splitLines(active.text.toUpperCase());
  const withinWidth = width * 0.86;
  const fitted = Math.min(
    ...lines.map(
      (line) =>
        fitText({
          text: line,
          withinWidth,
          fontFamily: FONT_STACK,
          fontWeight: FONT_WEIGHT,
          letterSpacing: LETTER_SPACING,
          textTransform: "uppercase",
        }).fontSize,
    ),
  );
  // Cap by width fit, the giant-type ceiling, and the safe-region height.
  const heightCap = (SAFE_BOTTOM_Y * 0.66) / Math.max(1, lines.length) / 1.05;
  const fontSize = Math.max(1, Math.min(MAX_PX_CARD, fitted, heightCap));

  const textMotion = elementStyle(frame, fps, {
    from: "bottom",
    startSec: active.start,
    durSec: TEXT_ENTER_SEC,
    endSec: active.end - TEXT_LEAD_OUT_SEC,
    exitSec: TEXT_EXIT_SEC,
  });

  return (
    <AbsoluteFill style={{ background: "transparent" }}>
      {/* Full-bleed card background: quick fade, never slides. */}
      <AbsoluteFill style={{ background: palette.bg, opacity: cardOpacity }} />
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width,
          height: SAFE_BOTTOM_Y,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          opacity: cardOpacity,
        }}
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: fontSize * 0.18,
            maxWidth: withinWidth,
            ...textMotion,
          }}
        >
          {lines.map((line, i) => (
            <span
              key={i}
              style={{
                fontFamily: FONT_STACK,
                fontWeight: FONT_WEIGHT,
                fontSize,
                lineHeight: 1.02,
                letterSpacing: LETTER_SPACING,
                color: onBrand,
                textTransform: "uppercase",
                whiteSpace: "nowrap",
                textAlign: "center",
              }}
            >
              {line}
            </span>
          ))}
          <div
            style={{
              height: Math.max(8, fontSize * 0.09),
              width: "38%",
              background: palette.accent,
              borderRadius: 999,
              marginTop: fontSize * 0.1,
            }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};

export default HeroOverlay;
