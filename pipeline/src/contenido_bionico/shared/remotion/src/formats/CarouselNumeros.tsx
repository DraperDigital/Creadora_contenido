/**
 * Big-number listicle carousel (1080x1350 PNG stills, one per frame).
 *
 * Frame 0 is the hero (title + "N claves"); frames 1..N each render one key
 * point with a giant outlined 2-digit number (01, 02, …) in the accent color; an
 * optional final `cta` frame closes with a follow/save call to action. When a
 * `bgSrc` background canvas is passed it renders FULL-BLEED behind every frame
 * and the text flips to a LIGHT palette on a dark canvas (`darkBg`). Poppins only
 * (latin subset) — the page indicator is CSS dots, never a glyph.
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

type Palette = { bg: string; ink: string; paper: string; accent: string; accentSoft: string };
type Slide = { kind: string; heading?: string; body?: string; index?: number; total?: number };
type Props = {
  title: string;
  eyebrow: string;
  slides: Slide[];
  palette: Palette;
  bgSrc?: string;
  darkBg?: boolean;
};

const FONT = `${BRAND_FONT_FAMILY}, sans-serif`;

// Full-bleed background canvas behind the slide; null when no bgSrc (the comp
// then keeps its flat palette background).
const Bg: React.FC<{ src?: string }> = ({ src }) =>
  src ? (
    <AbsoluteFill>
      <Img src={staticFile(src)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
    </AbsoluteFill>
  ) : null;

// Shared follow/save closing slide, rendered over the background (accent fill
// when there is no background). Text flips light/dark to match the canvas.
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

const Dots: React.FC<{ total: number; active: number; on: string; off: string }> = ({
  total,
  active,
  on,
  off,
}) => (
  <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
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

const clamp = (lines: number): React.CSSProperties => ({
  display: "-webkit-box",
  WebkitLineClamp: lines,
  WebkitBoxOrient: "vertical",
  overflow: "hidden",
});

export const CarouselNumeros: React.FC<Props> = ({ title, eyebrow, slides, palette, bgSrc, darkBg }) => {
  const frame = useCurrentFrame();
  const items = slides.filter((s) => s.kind === "item");
  const total = items.length;
  const onBg = !!bgSrc;
  const ink = onBg ? (darkBg ? palette.paper : palette.ink) : palette.ink;
  const base: React.CSSProperties = {
    backgroundColor: onBg ? "transparent" : palette.paper,
    color: ink,
    fontFamily: FONT,
    padding: "112px 96px",
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
            <div style={{ fontSize: 118, fontWeight: 900, lineHeight: 1.02, letterSpacing: -2, ...clamp(5) }}>
              {title}
            </div>
          </div>
          <div style={{ fontSize: 42, fontWeight: 600, opacity: 0.6 }}>
            {total} {total === 1 ? "clave" : "claves"}
          </div>
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  const current = slides[Math.min(frame - 1, slides.length - 1)];
  if (current?.kind === "cta") {
    return <Cta heading={current.heading} body={current.body} palette={palette} bgSrc={bgSrc} darkBg={darkBg} />;
  }

  const idx = Math.min(frame - 1, items.length - 1);
  const s = items[idx];
  const num = String(s?.index ?? idx + 1).padStart(2, "0");

  return (
    <AbsoluteFill>
      <Bg src={bgSrc} />
      <AbsoluteFill style={base}>
        <div
          style={{
            fontSize: 300,
            fontWeight: 900,
            lineHeight: 0.86,
            letterSpacing: -8,
            color: "transparent",
            WebkitTextStroke: `5px ${palette.accent}`,
          }}
        >
          {num}
        </div>
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
            gap: 34,
          }}
        >
          <div style={{ fontSize: 82, fontWeight: 900, lineHeight: 1.05, letterSpacing: -1, ...clamp(3) }}>
            {s?.heading}
          </div>
          {s?.body ? (
            <div style={{ fontSize: 46, fontWeight: 400, lineHeight: 1.4, opacity: 0.78, ...clamp(5) }}>
              {s.body}
            </div>
          ) : null}
        </div>
        <Dots total={total} active={idx} on={palette.accent} off={palette.accentSoft} />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export default CarouselNumeros;
