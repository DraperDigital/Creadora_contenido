/**
 * Editorial / magazine carousel (1080x1350 PNG stills, one per frame).
 *
 * Frame 0 hero (kicker + serif display title); item frames use a serif heading,
 * an italic serif body, thin rules top and bottom, and a folio (page number); an
 * optional final `cta` frame closes with a follow/save call to action. When a
 * `bgSrc` background canvas is passed it renders FULL-BLEED behind every frame
 * and the type/rules flip to a LIGHT palette on a dark canvas (`darkBg`). This
 * comp deliberately uses a SYSTEM serif stack (Georgia) for its display type —
 * matching the classic carousel's system-serif body — while kickers/folios stay
 * in Poppins.
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

const SANS = `${BRAND_FONT_FAMILY}, sans-serif`;
const SERIF = 'Georgia, "Times New Roman", serif';

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
          fontFamily: SANS,
          padding: "120px 96px",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          textAlign: "center",
          gap: 44,
        }}
      >
        <div style={{ fontFamily: SERIF, fontSize: 108, fontWeight: 700, lineHeight: 1.06, letterSpacing: -1 }}>
          {heading}
        </div>
        <div style={{ width: 168, height: 6, backgroundColor: line }} />
        {body ? (
          <div style={{ fontFamily: SERIF, fontStyle: "italic", fontSize: 48, fontWeight: 400, lineHeight: 1.42, opacity: 0.9, maxWidth: 780 }}>
            {body}
          </div>
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

export const CarouselEditorial: React.FC<Props> = ({ title, eyebrow, slides, palette, bgSrc, darkBg }) => {
  const frame = useCurrentFrame();
  const items = slides.filter((s) => s.kind === "item");
  const total = items.length;
  const onBg = !!bgSrc;
  const ink = onBg ? (darkBg ? palette.paper : palette.ink) : palette.ink;
  const rule = `2px solid ${ink}`;
  const base: React.CSSProperties = {
    backgroundColor: onBg ? "transparent" : palette.paper,
    color: ink,
    fontFamily: SANS,
    padding: "104px 96px",
    display: "flex",
    flexDirection: "column",
  };
  const kicker: React.CSSProperties = {
    fontSize: 36,
    fontWeight: 700,
    letterSpacing: 6,
    textTransform: "uppercase",
    color: palette.accent,
  };
  const folioRow: React.CSSProperties = {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    fontSize: 34,
    fontWeight: 600,
    letterSpacing: 2,
    opacity: 0.6,
  };

  if (frame <= 0) {
    return (
      <AbsoluteFill>
        <Bg src={bgSrc} />
        <AbsoluteFill style={{ ...base, justifyContent: "space-between" }}>
          <div style={{ borderTop: rule, paddingTop: 28, display: "flex", justifyContent: "space-between" }}>
            <span style={kicker}>{eyebrow}</span>
            <span style={{ ...kicker, color: ink, opacity: 0.5 }}>Edición</span>
          </div>
          <div style={{ flex: 1, display: "flex", alignItems: "center" }}>
            <div style={{ fontFamily: SERIF, fontSize: 128, fontWeight: 700, lineHeight: 1.02, letterSpacing: -2, ...clamp(6) }}>
              {title}
            </div>
          </div>
          <div style={{ ...folioRow, borderTop: rule, paddingTop: 28 }}>
            <span>Desliza</span>
            <span>{total} páginas</span>
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
  const folio = String(s?.index ?? idx + 1).padStart(2, "0");

  return (
    <AbsoluteFill>
      <Bg src={bgSrc} />
      <AbsoluteFill style={base}>
        <div style={{ ...folioRow, borderTop: rule, paddingTop: 28 }}>
          <span style={kicker}>{eyebrow}</span>
          <span>{folio}</span>
        </div>
        <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", gap: 44 }}>
          <div style={{ fontFamily: SERIF, fontSize: 92, fontWeight: 700, lineHeight: 1.08, letterSpacing: -1, ...clamp(4) }}>
            {s?.heading}
          </div>
          {s?.body ? (
            <>
              <div style={{ width: 160, height: 3, backgroundColor: palette.accent }} />
              <div
                style={{
                  fontFamily: SERIF,
                  fontStyle: "italic",
                  fontSize: 52,
                  fontWeight: 400,
                  lineHeight: 1.42,
                  opacity: 0.82,
                  ...clamp(6),
                }}
              >
                {s.body}
              </div>
            </>
          ) : null}
        </div>
        <div style={{ ...folioRow, borderTop: rule, paddingTop: 28 }}>
          <span>{title}</span>
          <span>
            {idx + 1} / {total}
          </span>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export default CarouselEditorial;
