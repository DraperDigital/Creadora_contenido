/**
 * KaraokeQuote — a verbatim quote whose words light up one-by-one, synced to the
 * creator's REAL voice for that transcript span (muxed by the producer).
 *
 * Opaque 1080x1920. The quote is wrapped into lines of <= 7 words (Poppins, the
 * bundled heavy weight). Every word rests DIM and, at its own `start` (span-
 * relative seconds), resolves to FULL with the operator motion language's
 * blur + fade-in character on the expo ease-out — the WORD-SYNC EXCEPTION: no
 * slide travel, and each word's entrance is capped to its own sync gap (the
 * time until the next word lights, <= 250ms) so the reveal never lags the
 * voice. The dim/bright crossfade keeps the snap reading as a color/opacity
 * change, not a flash. A progress underline sweeps left-to-right with the
 * voice; after the last word the whole quote dwells lit for the 0.8s tail the
 * producer appends.
 *
 * Colors come from `palette`; the text color is chosen for contrast against
 * `palette.bg` (white on dark backgrounds, dark ink on light ones) so the dim
 * resting state stays legible whatever the brand palette is. Latin subset only
 * (Spanish, U+0000-00FF) — no emoji.
 */
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadBrandFonts } from "../lib/fonts";
import { easeOutExpo } from "./motion";

// Standalone isolated entrypoint: register the brand fonts here too.
loadBrandFonts();

type Word = { word: string; start: number; end: number };

type Palette = {
  bg: string;
  ink: string;
  paper: string;
  accent: string;
  accentSoft: string;
};

type KaraokeQuoteProps = {
  text: string;
  words: Word[];
  durationSec: number;
  palette: Palette;
};

// 900 = Poppins Black, the bundled heavy weight (only 400/600/900 are loaded;
// the spec's "800" resolves to this — matches the Captions decision).
const WORD_WEIGHT = 900;
const MAX_WORDS_PER_LINE = 7;
// Per-word reveal (motion-language word-sync exception): blur + fade on the
// expo ease-out, capped to the word's own sync gap, never over 250ms.
const WORD_ENTER_CAP_SEC = 0.25;
const WORD_ENTER_MIN_SEC = 0.06;
const WORD_BLUR_PX = 8;
const DIM_OPACITY = 0.25;
const SAFE_BOTTOM_RATIO = 0.2; // reserve the platform's bottom-UI zone

const hexToRgb = (hex: string): [number, number, number] => {
  let h = (hex || "").replace("#", "").trim();
  if (h.length === 3) {
    h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  }
  const n = Number.parseInt(h.slice(0, 6) || "000000", 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};

const relLuminance = (hex: string): number => {
  const [r, g, b] = hexToRgb(hex).map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

const rgba = (hex: string, alpha: number): string => {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
};

// Largest legible size at which the wrapped quote fits the safe area.
const fontPxFor = (count: number): number => {
  if (count <= 12) return 88;
  if (count <= 20) return 74;
  if (count <= 30) return 62;
  return 52;
};

const wrapLines = (words: Word[]): Word[][] => {
  const lines: Word[][] = [];
  for (let i = 0; i < words.length; i += MAX_WORDS_PER_LINE) {
    lines.push(words.slice(i, i + MAX_WORDS_PER_LINE));
  }
  return lines;
};

export const KaraokeQuote: React.FC<KaraokeQuoteProps> = ({
  words,
  durationSec,
  palette,
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const time = frame / fps;

  const readable = relLuminance(palette.bg) > 0.4 ? "#141414" : "#ffffff";
  const fontSize = fontPxFor(words.length);
  const lines = wrapLines(words);

  // Each word's entrance length = its sync gap (time until the next word
  // starts; its own span for the last word), clamped to [MIN, CAP] so the
  // reveal keeps the blur+fade character without ever lagging the voice.
  const enterDur = words.map((w, i) => {
    const gap = i + 1 < words.length ? words[i + 1].start - w.start : w.end - w.start;
    return Math.min(WORD_ENTER_CAP_SEC, Math.max(WORD_ENTER_MIN_SEC, gap));
  });

  // Underline sweep tracks the voice: full at the last word's end, then holds
  // through the tail dwell.
  const voiceEnd = words.length
    ? Math.max(...words.map((w) => w.end))
    : Math.max(0.001, durationSec);
  const progress = interpolate(time, [0, voiceEnd], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const outer: React.CSSProperties = {
    backgroundColor: palette.bg,
    display: "flex",
    flexDirection: "column",
    justifyContent: "center",
    alignItems: "center",
    // Center within the TOP 80% only (bottom 20% is the platform UI zone).
    paddingBottom: `${SAFE_BOTTOM_RATIO * 100}%`,
    paddingLeft: width * 0.075,
    paddingRight: width * 0.075,
    fontFamily: "'Poppins'",
  };

  const block: React.CSSProperties = {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    rowGap: fontSize * 0.14,
    width: "100%",
  };

  const rowStyle: React.CSSProperties = {
    display: "flex",
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "center",
    columnGap: fontSize * 0.3,
    lineHeight: 1.12,
  };

  return (
    <AbsoluteFill style={outer}>
      <div style={block}>
        {lines.map((line, li) => (
          <div key={li} style={rowStyle}>
            {line.map((w, wi) => {
              const gi = li * MAX_WORDS_PER_LINE + wi;
              // Motion-language word reveal: blur + fade-in on the expo curve,
              // capped to the sync gap. No slide (word-sync exception).
              const enter = easeOutExpo((time - w.start) / enterDur[gi]);
              const wrap: React.CSSProperties = {
                position: "relative",
                display: "inline-block",
                fontWeight: WORD_WEIGHT,
                fontSize,
              };
              const dim: React.CSSProperties = {
                color: readable,
                opacity: DIM_OPACITY * (1 - enter),
                whiteSpace: "pre",
              };
              const bright: React.CSSProperties = {
                position: "absolute",
                left: 0,
                top: 0,
                color: readable,
                opacity: enter,
                filter:
                  enter > 0 && enter < 1
                    ? `blur(${((1 - enter) * WORD_BLUR_PX).toFixed(2)}px)`
                    : undefined,
                textShadow: `0 0 ${fontSize * 0.22}px ${rgba(palette.accent, 0.45)}`,
                whiteSpace: "pre",
              };
              return (
                <span key={wi} style={wrap}>
                  <span style={dim}>{w.word}</span>
                  <span style={bright}>{w.word}</span>
                </span>
              );
            })}
          </div>
        ))}

        {/* Progress underline sweep. */}
        <div
          style={{
            marginTop: fontSize * 0.55,
            width: "58%",
            height: Math.max(6, Math.round(height * 0.005)),
            borderRadius: 999,
            backgroundColor: rgba(readable, 0.14),
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: "100%",
              height: "100%",
              backgroundColor: palette.accent,
              transform: `scaleX(${progress})`,
              transformOrigin: "left center",
            }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};

export default KaraokeQuote;
