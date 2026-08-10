/**
 * Photo carousel — classic key points laid over the operator's photos
 * (1080x1350 PNG stills, one per frame).
 *
 * Frame 0 hero (hero photo + title); item frames render each point over its
 * staged photo. Every photo gets a dark bottom-up gradient scrim so the
 * bottom-left heading/body stay legible. Photos are staged by the producer into
 * public/assets/runs/<id>/broll/ and referenced via staticFile(photoSrc).
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

type Palette = { bg: string; ink: string; paper: string; accent: string; accentSoft: string };
type Slide = { kind: string; heading?: string; body?: string; index?: number; total?: number; photoSrc?: string };
type Props = {
  title: string;
  eyebrow: string;
  slides: Slide[];
  palette: Palette;
  heroPhotoSrc?: string;
};

const FONT = `${BRAND_FONT_FAMILY}, sans-serif`;
const TEXT = "#FFFFFF";
// The photo sits at half opacity over a dark base so the whole image is
// darkened uniformly and the white text stays highlighted everywhere (not only
// where the bottom scrim reaches). A fixed near-black base guarantees the
// darkening regardless of how light the brand palette's bg is.
const PHOTO_OPACITY = 0.5;
const DARK_BASE = "#0E0E10";
const SCRIM =
  "linear-gradient(to top, rgba(0,0,0,0.86) 0%, rgba(0,0,0,0.55) 34%, rgba(0,0,0,0.12) 62%, rgba(0,0,0,0) 100%)";

const clamp = (lines: number): React.CSSProperties => ({
  display: "-webkit-box",
  WebkitLineClamp: lines,
  WebkitBoxOrient: "vertical",
  overflow: "hidden",
});

const Dots: React.FC<{ total: number; active: number; on: string }> = ({ total, active, on }) => (
  <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
    {Array.from({ length: total }).map((_, i) => (
      <div
        key={i}
        style={{
          width: i === active ? 46 : 16,
          height: 16,
          borderRadius: 8,
          backgroundColor: i === active ? on : "rgba(255,255,255,0.45)",
        }}
      />
    ))}
  </div>
);

const Bg: React.FC<{ src?: string }> = ({ src }) => (
  <>
    <AbsoluteFill style={{ backgroundColor: DARK_BASE }} />
    {src ? (
      // The Img must live inside a positioned AbsoluteFill: a static <img>
      // sibling paints BELOW the positioned bg/scrim fills regardless of DOM
      // order, which hid the photo entirely. Half opacity over the dark base
      // darkens the photo so the text on top reads clearly.
      <AbsoluteFill style={{ opacity: PHOTO_OPACITY }}>
        <Img src={staticFile(src)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      </AbsoluteFill>
    ) : null}
    <AbsoluteFill style={{ background: SCRIM }} />
  </>
);

export const CarouselFotos: React.FC<Props> = ({ title, eyebrow, slides, palette, heroPhotoSrc }) => {
  const frame = useCurrentFrame();
  const items = slides.filter((s) => s.kind === "item");
  const total = items.length;
  const pad = "96px 88px";

  if (frame <= 0) {
    return (
      <AbsoluteFill style={{ fontFamily: FONT, color: TEXT }}>
        <Bg src={heroPhotoSrc ?? items[0]?.photoSrc} />
        <AbsoluteFill style={{ padding: pad, display: "flex", flexDirection: "column", justifyContent: "flex-end", gap: 28 }}>
          <div style={{ fontSize: 40, fontWeight: 700, letterSpacing: 8, textTransform: "uppercase", color: palette.accentSoft }}>
            {eyebrow}
          </div>
          <div style={{ fontSize: 116, fontWeight: 900, lineHeight: 1.02, letterSpacing: -2, ...clamp(5) }}>
            {title}
          </div>
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  const idx = Math.min(frame - 1, items.length - 1);
  const s = items[idx];

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: TEXT }}>
      <Bg src={s?.photoSrc} />
      <AbsoluteFill style={{ padding: pad, display: "flex", flexDirection: "column", justifyContent: "flex-end", gap: 32 }}>
        <div
          style={{
            alignSelf: "flex-start",
            fontSize: 60,
            fontWeight: 900,
            lineHeight: 1,
            color: palette.bg,
            backgroundColor: palette.accent,
            borderRadius: 20,
            padding: "12px 28px",
          }}
        >
          {String(s?.index ?? idx + 1).padStart(2, "0")}
        </div>
        <div style={{ fontSize: 78, fontWeight: 900, lineHeight: 1.05, letterSpacing: -1, ...clamp(3) }}>
          {s?.heading}
        </div>
        {s?.body ? (
          <div style={{ fontSize: 46, fontWeight: 400, lineHeight: 1.38, opacity: 0.92, ...clamp(4) }}>
            {s.body}
          </div>
        ) : null}
        <Dots total={total} active={idx} on={palette.accent} />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export default CarouselFotos;
