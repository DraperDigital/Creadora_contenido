/**
 * Checklist "guarda esto" carousel (1080x1350 PNG stills, one per frame).
 *
 * Frame 0 hero (title + eyebrow); item frames render a large accent check box
 * (SVG tick — never an emoji/glyph) + heading + body; the final frame is a
 * "Guarda este post" CTA. When a `bgSrc` background canvas is passed it renders
 * FULL-BLEED behind every frame and the text flips to a LIGHT palette on a dark
 * canvas (`darkBg`); otherwise the light-paper / dark-ink flat treatment is kept.
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

const Bg: React.FC<{ src?: string }> = ({ src }) =>
  src ? (
    <AbsoluteFill>
      <Img src={staticFile(src)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
    </AbsoluteFill>
  ) : null;

const clamp = (lines: number): React.CSSProperties => ({
  display: "-webkit-box",
  WebkitLineClamp: lines,
  WebkitBoxOrient: "vertical",
  overflow: "hidden",
});

const CheckBox: React.FC<{ size: number; box: string; tick: string }> = ({ size, box, tick }) => (
  <div
    style={{
      width: size,
      height: size,
      borderRadius: size * 0.24,
      backgroundColor: box,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      flexShrink: 0,
    }}
  >
    <svg width={size * 0.62} height={size * 0.62} viewBox="0 0 24 24" fill="none">
      <path
        d="M4.5 12.5l4.5 4.5L19.5 6.5"
        stroke={tick}
        strokeWidth={3.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  </div>
);

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

export const CarouselChecklist: React.FC<Props> = ({ title, eyebrow, slides, palette, bgSrc, darkBg }) => {
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
            <div style={{ fontSize: 116, fontWeight: 900, lineHeight: 1.03, letterSpacing: -2, ...clamp(5) }}>
              {title}
            </div>
          </div>
          <div style={{ fontSize: 42, fontWeight: 600, opacity: 0.6 }}>Checklist para guardar</div>
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  const idx = Math.min(frame - 1, slides.length - 1);
  const s = slides[idx];

  if (s?.kind === "cta") {
    const ctaText = onBg ? (darkBg ? palette.paper : palette.ink) : palette.paper;
    return (
      <AbsoluteFill>
        <Bg src={bgSrc} />
        <AbsoluteFill
          style={{
            ...base,
            backgroundColor: onBg ? "transparent" : palette.accent,
            color: ctaText,
            alignItems: "center",
            justifyContent: "center",
            textAlign: "center",
            gap: 48,
          }}
        >
          <CheckBox
            size={220}
            box={onBg ? palette.accent : palette.paper}
            tick={onBg ? palette.paper : palette.accent}
          />
          <div style={{ fontSize: 104, fontWeight: 900, lineHeight: 1.05, letterSpacing: -1 }}>
            {s.heading}
          </div>
          {s.body ? (
            <div style={{ fontSize: 46, fontWeight: 400, lineHeight: 1.4, opacity: 0.9, maxWidth: 760 }}>
              {s.body}
            </div>
          ) : null}
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  return (
    <AbsoluteFill>
      <Bg src={bgSrc} />
      <AbsoluteFill style={base}>
        <Dots total={total} active={idx} on={palette.accent} off={palette.accentSoft} />
        <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 48 }}>
          <CheckBox size={150} box={palette.accent} tick={palette.paper} />
          <div style={{ display: "flex", flexDirection: "column", gap: 26 }}>
            <div style={{ fontSize: 76, fontWeight: 900, lineHeight: 1.05, letterSpacing: -1, ...clamp(3) }}>
              {s?.heading}
            </div>
            {s?.body ? (
              <div style={{ fontSize: 46, fontWeight: 400, lineHeight: 1.4, opacity: 0.78, ...clamp(5) }}>
                {s.body}
              </div>
            ) : null}
          </div>
        </div>
        <div style={{ fontSize: 40, fontWeight: 600, opacity: 0.55 }}>
          {idx + 1} / {total}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export default CarouselChecklist;
