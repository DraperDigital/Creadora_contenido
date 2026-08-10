/**
 * Myth-vs-truth carousel (1080x1350 PNG stills, one per frame).
 *
 * Frame 0 hero (title + eyebrow); each par frame stacks two panels: a muted
 * MITO panel (the misconception, struck through in the accent) over an accent
 * REALIDAD panel (the real point); an optional final `cta` frame closes with a
 * follow/save call to action. When a `bgSrc` background canvas is passed it
 * renders FULL-BLEED behind every frame and the hero/footer text flips to a
 * LIGHT palette on a dark canvas (`darkBg`); the panels are self-contained cards
 * that read on any canvas. Poppins only; page indicator is CSS dots.
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

type Palette = { bg: string; ink: string; paper: string; accent: string; accentSoft: string };
type Slide = { kind: string; mito?: string; realidad?: string; heading?: string; body?: string; index?: number; total?: number };
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

const clamp = (lines: number): React.CSSProperties => ({
  display: "-webkit-box",
  WebkitLineClamp: lines,
  WebkitBoxOrient: "vertical",
  overflow: "hidden",
});

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

const Label: React.FC<{ text: string; color: string }> = ({ text, color }) => (
  <div style={{ fontSize: 38, fontWeight: 700, letterSpacing: 6, textTransform: "uppercase", color }}>
    {text}
  </div>
);

export const CarouselMitos: React.FC<Props> = ({ title, eyebrow, slides, palette, bgSrc, darkBg }) => {
  const frame = useCurrentFrame();
  const pares = slides.filter((s) => s.kind === "par");
  const total = pares.length;
  const onBg = !!bgSrc;
  const ink = onBg ? (darkBg ? palette.paper : palette.ink) : palette.ink;
  const base: React.CSSProperties = {
    backgroundColor: onBg ? "transparent" : palette.paper,
    color: ink,
    fontFamily: FONT,
    padding: "104px 88px",
    display: "flex",
    flexDirection: "column",
    justifyContent: "space-between",
  };

  if (frame <= 0) {
    return (
      <AbsoluteFill>
        <Bg src={bgSrc} />
        <AbsoluteFill style={base}>
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
            <div style={{ fontSize: 112, fontWeight: 900, lineHeight: 1.03, letterSpacing: -2, ...clamp(5) }}>
              {title}
            </div>
          </div>
          <div style={{ fontSize: 42, fontWeight: 600, opacity: 0.6 }}>Mito vs realidad</div>
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  const current = slides[Math.min(frame - 1, slides.length - 1)];
  if (current?.kind === "cta") {
    return <Cta heading={current.heading} body={current.body} palette={palette} bgSrc={bgSrc} darkBg={darkBg} />;
  }

  const idx = Math.min(frame - 1, pares.length - 1);
  const s = pares[idx];

  return (
    <AbsoluteFill>
      <Bg src={bgSrc} />
      <AbsoluteFill style={base}>
        <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", gap: 40 }}>
          <div
            style={{
              backgroundColor: palette.accentSoft,
              borderRadius: 36,
              padding: "48px 52px",
              display: "flex",
              flexDirection: "column",
              gap: 22,
            }}
          >
            <Label text="Mito" color={palette.accent} />
            <div
              style={{
                fontSize: 54,
                fontWeight: 600,
                lineHeight: 1.28,
                color: palette.ink,
                opacity: 0.72,
                textDecoration: "line-through",
                textDecorationColor: palette.accent,
                ...clamp(4),
              }}
            >
              {s?.mito}
            </div>
          </div>
          <div
            style={{
              backgroundColor: palette.accent,
              borderRadius: 36,
              padding: "48px 52px",
              display: "flex",
              flexDirection: "column",
              gap: 22,
            }}
          >
            <Label text="Realidad" color={palette.paper} />
            <div style={{ fontSize: 60, fontWeight: 800, lineHeight: 1.24, color: palette.paper, ...clamp(5) }}>
              {s?.realidad}
            </div>
          </div>
        </div>
        <Dots total={total} active={idx} on={palette.accent} off={palette.accentSoft} />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export default CarouselMitos;
