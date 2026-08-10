/**
 * QuoteTecleado — a quote "typed in" (tecleado) as a 1080x1920 video, with a
 * highlighter-pen swipe animating in behind the key word(s) once they finish
 * typing. Character-by-character reveal over [0, typeEndSec], a blinking cursor
 * on the growing edge, then a hold. Poppins only (the embedded font). Latin-1.
 */
import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { fitText } from "@remotion/layout-utils";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

const W = 1080;
const H = 1920;
const FONT = `'${BRAND_FONT_FAMILY}', sans-serif`;
const WEIGHT = 800;

type Palette = { bg: string; ink: string; paper: string; accent: string; accentSoft: string };
type Props = { text: string; highlights?: string[]; typeEndSec?: number; palette: Palette };

const normWord = (w: string): string =>
  w.toLowerCase().replace(/[^0-9a-záéíóúñü]/gi, "");

export const QuoteTecleado: React.FC<Props> = ({ text, highlights = [], typeEndSec = 6, palette }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;

  const clean = (text ?? "").trim() || " ";
  const words = clean.split(/\s+/).filter(Boolean);
  const totalChars = clean.length;
  const typeDur = Math.max(0.3, typeEndSec);
  // Characters typed so far (across the whole quote), + a blinking cursor.
  const typed = Math.min(totalChars, Math.ceil((t / typeDur) * totalChars));
  const typing = t < typeDur;
  const cursorOn = Math.floor(t * 2) % 2 === 0; // ~2 Hz blink

  // One shared size so the wrapped block fits the column (measured on the full
  // quote, so it never reflows as characters appear).
  const withinWidth = (W - 180) * 2.4; // account for wrapping across ~2-3 lines
  const fitted = fitText({
    text: clean,
    withinWidth,
    fontFamily: BRAND_FONT_FAMILY,
    fontWeight: String(WEIGHT),
    letterSpacing: "-0.02em",
  }).fontSize;
  const fontSize = Math.max(52, Math.min(120, fitted));

  const hlSet = new Set(highlights.map(normWord).filter(Boolean));

  // Walk words tracking the char offset so we can reveal char-by-char and
  // highlight a word only once it is fully typed.
  let offset = 0;
  const spans: React.ReactNode[] = [];
  words.forEach((w, i) => {
    const start = offset;
    offset += w.length + 1; // +1 for the space that follows
    const shown = Math.max(0, Math.min(w.length, typed - start));
    if (shown <= 0) return;
    const fullyTyped = shown >= w.length;
    const isHl = fullyTyped && hlSet.has(normWord(w));
    const wordAppeared = (start + w.length) / totalChars * typeDur; // ~when it finished
    const swipe = isHl
      ? interpolate(t, [wordAppeared + 0.05, wordAppeared + 0.45], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : 0;
    spans.push(
      <span key={i} style={{ position: "relative", display: "inline-block", margin: "0 0.14em" }}>
        {isHl ? (
          <span
            style={{
              position: "absolute",
              left: "-0.08em",
              right: "-0.08em",
              top: "0.16em",
              bottom: "0.04em",
              background: palette.accent,
              opacity: 0.85,
              borderRadius: 8,
              transform: `scaleX(${swipe})`,
              transformOrigin: "left center",
              zIndex: 0,
            }}
          />
        ) : null}
        <span style={{ position: "relative", zIndex: 1 }}>{w.slice(0, shown)}</span>
      </span>,
    );
  });

  return (
    <AbsoluteFill
      style={{
        backgroundColor: palette.bg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "0 90px",
      }}
    >
      <div
        style={{
          fontFamily: FONT,
          fontWeight: WEIGHT,
          fontSize,
          lineHeight: 1.18,
          letterSpacing: "-0.02em",
          color: palette.paper,
          textAlign: "center",
          maxWidth: W - 160,
        }}
      >
        {spans}
        {/* Blinking cursor on the growing edge while typing. */}
        {typing && cursorOn ? (
          <span style={{ display: "inline-block", width: "0.5em", color: palette.accent }}>|</span>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};

export default QuoteTecleado;
