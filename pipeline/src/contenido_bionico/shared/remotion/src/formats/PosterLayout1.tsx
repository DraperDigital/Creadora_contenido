/**
 * PosterLayout1 — element distribution only (no theme). Eyebrow keywords + a
 * giant headline fill the TOP (behind the subject); a bold points block, a
 * quote and index furniture sit at the BOTTOM (in front, over the body).
 * All text comes from the shared neutral content. Solid or foto background.
 */
import React from "react";
import {
  POSTER_FONT,
  Palette,
  PosterFrame,
  GiantStack,
  Barcode,
  Crosshair,
  Micro,
  lightTextShadow,
} from "./posterKit";

const BG = "#141210";
const CREAM = "#EDE6D6";

type Content = {
  headline?: string[];
  keywords?: string[];
  points?: { heading: string; body: string }[];
  quote?: string;
  summary?: string;
};
type Props = {
  content: Content;
  mode: "solid" | "foto";
  subjectSrc?: string;
  bgSrc?: string;
  palette: Palette;
};

export const PosterLayout1: React.FC<Props> = ({ content, mode, subjectSrc, bgSrc, palette }) => {
  const accent = palette.accent;
  const kw = (content.keywords ?? []).filter(Boolean);
  const eyebrow = kw.join("   //   ");
  const points = (content.points ?? []).map((p) => p.heading).filter(Boolean).slice(0, 3);
  const quote = (content.quote ?? "").trim();

  const behind = (
    <>
      {eyebrow ? (
        <Micro color={accent} size={24} tracking="0.28em" weight={600}
          style={{ position: "absolute", top: 46, left: 56, right: 56, textAlign: "center" }}>
          {eyebrow}
        </Micro>
      ) : null}
      <GiantStack lines={content.headline ?? []} color={CREAM} widthPct={0.98} condense={0.8}
        lineHeight={0.86} style={{ position: "absolute", top: 92, left: 40, right: 40 }} />
    </>
  );

  const front = (
    <>
      {points.length ? (
        <div style={{ position: "absolute", left: 56, top: 828 }}>
          {points.map((line, i) => (
            <div key={i} style={{
              fontFamily: POSTER_FONT, fontWeight: 800, fontSize: 46, lineHeight: 1.04,
              letterSpacing: "-0.01em", textTransform: "uppercase",
              color: i === points.length - 1 ? accent : CREAM,
              textShadow: lightTextShadow(i === points.length - 1 ? accent : CREAM),
            }}>{line}</div>
          ))}
          <div style={{ width: 70, height: 6, backgroundColor: accent, marginTop: 22 }} />
        </div>
      ) : null}

      <div style={{ position: "absolute", right: 40, top: 900, display: "flex", alignItems: "flex-start", gap: 14 }}>
        <Barcode color={CREAM} w={120} h={44} />
        <div style={{
          fontFamily: POSTER_FONT, fontWeight: 600, fontSize: 18, letterSpacing: "0.24em",
          color: CREAM, textTransform: "uppercase", writingMode: "vertical-rl",
          textShadow: lightTextShadow(CREAM),
        }}>Serie 01</div>
      </div>

      <Crosshair color={accent} size={30} style={{ position: "absolute", left: 56, bottom: 150 }} />
      {quote ? (
        <div style={{
          position: "absolute", left: 90, right: 90, bottom: 66, textAlign: "center",
          fontFamily: POSTER_FONT, fontWeight: 600, fontSize: 22, letterSpacing: "0.05em",
          lineHeight: 1.4, textTransform: "uppercase", color: CREAM,
          textShadow: lightTextShadow(CREAM),
        }}>{quote}</div>
      ) : null}
      <Micro color={accent} size={18} tracking="0.2em" style={{ position: "absolute", left: 56, bottom: 40 }}>
        Estd. MMXXIV
      </Micro>
    </>
  );

  return (
    <PosterFrame mode={mode} bgColor={BG} bgSrc={bgSrc} subjectSrc={subjectSrc}
      subjectTop={96} behind={behind} front={front} anim="slide" />
  );
};

export default PosterLayout1;
