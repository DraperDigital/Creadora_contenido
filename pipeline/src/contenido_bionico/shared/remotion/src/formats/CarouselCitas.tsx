/**
 * Quote carousel — one quote per slide (1080x1350 PNG stills, one per frame).
 *
 * Frame 0 hero (title); each quote frame centers one oversized quote between
 * accent guillemets (« », latin-1, so they render in Poppins); an optional final
 * `cta` frame closes with a follow/save call to action. Dark background, paper
 * text by default; when a `bgSrc` background canvas is passed it renders
 * FULL-BLEED behind every frame and the text flips light/dark to match the
 * canvas (`darkBg`). Quote size steps down with length so long quotes still fit.
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

type Palette = { bg: string; ink: string; paper: string; accent: string; accentSoft: string };
type Slide = { kind: string; body?: string; heading?: string; index?: number; total?: number };
type Props = {
  title: string;
  eyebrow: string;
  slides: Slide[];
  palette: Palette;
  bgSrc?: string;
  darkBg?: boolean;
};

const FONT = `${BRAND_FONT_FAMILY}, sans-serif`;

const Bg: React.FC<{ src?: string }> = ({ src }) =>
  src ? (
    <AbsoluteFill>
      <Img src={staticFile(src)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
    </AbsoluteFill>
  ) : null;

const Cta: React.FC<{
  heading?: string;
  body?: string;
  palette: Palette;
  bgSrc?: string;
  darkBg?: boolean;
}> = ({ heading, body, palette, bgSrc, darkBg }) => {
  const onBg = !!bgSrc;
  const text = onBg ? (darkBg ? palette.paper : palette.ink) : palette.paper;
  const line = onBg ? palette.accent : palette.paper;
  return (
    <AbsoluteFill>
      <Bg src={bgSrc} />
      <AbsoluteFill
        style={{
          backgroundColor: onBg ? "transparent" : palette.accent,
          color: text,
          fontFamily: FONT,
          padding: "120px 96px",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          textAlign: "center",
          gap: 44,
        }}
      >
        <div style={{ fontSize: 104, fontWeight: 900, lineHeight: 1.06, letterSpacing: -1 }}>{heading}</div>
        <div style={{ width: 168, height: 8, borderRadius: 4, backgroundColor: line }} />
        {body ? (
          <div style={{ fontSize: 46, fontWeight: 400, lineHeight: 1.42, opacity: 0.9, maxWidth: 780 }}>{body}</div>
        ) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const quoteSize = (text: string): number => {
  const n = text.length;
  if (n > 170) return 54;
  if (n > 120) return 64;
  if (n > 74) return 76;
  return 92;
};

const Dots: React.FC<{ total: number; active: number; on: string; off: string }> = ({
  total,
  active,
  on,
  off,
}) => (
  <div style={{ display: "flex", gap: 16, alignItems: "center", justifyContent: "center" }}>
    {Array.from({ length: total }).map((_, i) => (
      <div
        key={i}
        style={{
          width: i === active ? 46 : 16,
          height: 16,
          borderRadius: 8,
          backgroundColor: i === active ? on : off,
        }}
      />
    ))}
  </div>
);

export const CarouselCitas: React.FC<Props> = ({ title, eyebrow, slides, palette, bgSrc, darkBg }) => {
  const frame = useCurrentFrame();
  const quotes = slides.filter((s) => s.kind === "quote");
  const total = quotes.length;
  const onBg = !!bgSrc;
  const ink = onBg ? (darkBg ? palette.paper : palette.ink) : palette.paper;
  const base: React.CSSProperties = {
    backgroundColor: onBg ? "transparent" : palette.bg,
    color: ink,
    fontFamily: FONT,
    padding: "112px 96px",
    display: "flex",
    flexDirection: "column",
  };

  if (frame <= 0) {
    return (
      <AbsoluteFill>
        <Bg src={bgSrc} />
        <AbsoluteFill style={{ ...base, justifyContent: "space-between" }}>
          <div
            style={{
              fontSize: 40,
              fontWeight: 600,
              letterSpacing: 8,
              textTransform: "uppercase",
              color: palette.accent,
            }}
          >
            {eyebrow}
          </div>
          <div style={{ flex: 1, display: "flex", alignItems: "center" }}>
            <div style={{ fontSize: 112, fontWeight: 900, lineHeight: 1.04, letterSpacing: -2 }}>
              {title}
            </div>
          </div>
          <div style={{ fontSize: 42, fontWeight: 600, opacity: 0.55 }}>
            {total} {total === 1 ? "frase" : "frases"}
          </div>
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  const current = slides[Math.min(frame - 1, slides.length - 1)];
  if (current?.kind === "cta") {
    return <Cta heading={current.heading} body={current.body} palette={palette} bgSrc={bgSrc} darkBg={darkBg} />;
  }

  const idx = Math.min(frame - 1, quotes.length - 1);
  const s = quotes[idx];
  const text = s?.body ?? "";

  return (
    <AbsoluteFill>
      <Bg src={bgSrc} />
      <AbsoluteFill style={{ ...base, justifyContent: "center", alignItems: "center", textAlign: "center" }}>
        <div style={{ fontSize: 200, fontWeight: 900, lineHeight: 0.6, color: palette.accent, height: 120 }}>
          &laquo;
        </div>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div
            style={{
              fontSize: quoteSize(text),
              fontWeight: 800,
              lineHeight: 1.26,
              letterSpacing: -0.5,
              maxWidth: 860,
            }}
          >
            {text}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 40 }}>
          <div style={{ width: 120, height: 6, borderRadius: 3, backgroundColor: palette.accent }} />
          <Dots total={total} active={idx} on={palette.accent} off={palette.accentSoft} />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export default CarouselCitas;
