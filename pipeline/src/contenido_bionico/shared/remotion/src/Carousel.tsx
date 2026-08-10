/**
 * Carousel slides (4:5, 1080x1350) — rendered to PNG stills, one per frame.
 *
 * Adapted from the ranking tier-list carousel to a "key points" model: a
 * framework/explainer short has no tier list, so each content slide carries a
 * numbered step chip, a heading (the key idea) and a one-line body (the takeaway
 * in the creator's own words). Neutral palette, no emphasis color: bone
 * background, charcoal ink, muted gray labels, the body as a serif italic line.
 *
 * Frame N renders slides[N]: frame 0 is the hero title, frames 1..N-1 are one
 * key point each, and an optional final `cta` slide closes with a follow/save
 * line. Fonts are system faces (Segoe UI / Georgia) — present on the Windows
 * render host — so nothing needs bundling.
 *
 * The stories / square RATIO variants (short/formats) additionally pass an
 * optional `bgSrc` background canvas (rendered FULL-BLEED behind the slide) plus
 * a `darkBg` flag that flips the neutral palette to a LIGHT one so text reads on
 * a dark canvas. With neither prop the render is byte-identical to the classic
 * shipping carousel.
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import type {
  CarouselSlide,
  CarouselHeroSlide,
  CarouselItemSlide,
} from "./lib/types";

const BG = "#F3F1EC"; // bone / warm off-white
const INK = "#1C1B19"; // near-black charcoal
const MUTED = "#8A877F"; // labels / footers
const QUOTE = "#57544E"; // the body text
const RULE = "rgba(0,0,0,0.10)";

const SANS = '"Segoe UI", "Helvetica Neue", Arial, sans-serif';
const SERIF = 'Georgia, "Times New Roman", serif';

// The resolved slide colors. Defaults reproduce the classic neutral palette;
// on a dark background canvas they flip to light equivalents.
type Colors = {
  ink: string;
  muted: string;
  quote: string;
  rule: string;
  chipBg: string;
  chipText: string;
};

const LIGHT_COLORS: Colors = {
  ink: INK,
  muted: MUTED,
  quote: QUOTE,
  rule: RULE,
  chipBg: INK,
  chipText: BG,
};

// Local CTA slide shape (not part of the shipping CarouselSlide union): the
// ratio variants append it, the classic shipping carousel never does.
type CarouselCtaSlide = {
  kind: "cta";
  heading?: string;
  body?: string;
  index?: number;
  total?: number;
};
type AnySlide = CarouselSlide | CarouselCtaSlide;

type Props = {
  slides: AnySlide[];
  bgSrc?: string;
  darkBg?: boolean;
};

const frameStyleBase: React.CSSProperties = {
  padding: "120px 100px",
  display: "flex",
  flexDirection: "column",
  justifyContent: "space-between",
  fontFamily: SANS,
};

const footerBase: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  fontSize: 34,
  letterSpacing: 0.5,
};

const Bg: React.FC<{ src?: string }> = ({ src }) =>
  src ? (
    <AbsoluteFill>
      <Img src={staticFile(src)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
    </AbsoluteFill>
  ) : null;

const HeroSlide: React.FC<CarouselHeroSlide & { c: Colors }> = ({ eyebrow, title, footnote, c }) => (
  <>
    <div style={{ fontSize: 34, letterSpacing: 8, textTransform: "uppercase", color: c.muted }}>
      {eyebrow}
    </div>
    <div style={{ flex: 1, display: "flex", alignItems: "center" }}>
      <div style={{ fontSize: 116, fontWeight: 700, lineHeight: 1.04, letterSpacing: -1 }}>
        {title}
      </div>
    </div>
    <div style={{ ...footerBase, color: c.muted }}>
      <span>{footnote ?? ""}</span>
      <span>desliza &rarr;</span>
    </div>
  </>
);

const ItemSlide: React.FC<CarouselItemSlide & { c: Colors }> = ({ index, heading, body, total, c }) => (
  <>
    <div style={{ display: "flex", alignItems: "center", gap: 40 }}>
      <div
        style={{
          width: 168,
          height: 168,
          borderRadius: 28,
          backgroundColor: c.chipBg,
          color: c.chipText,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 96,
          fontWeight: 700,
          flexShrink: 0,
        }}
      >
        {index}
      </div>
      <div style={{ fontSize: 72, fontWeight: 700, lineHeight: 1.05, letterSpacing: -0.5 }}>
        {heading}
      </div>
    </div>

    <div style={{ flex: 1, display: "flex", alignItems: "center", padding: "60px 0" }}>
      {body ? (
        <div
          style={{
            fontFamily: SERIF,
            fontStyle: "italic",
            fontSize: 58,
            lineHeight: 1.42,
            color: c.quote,
          }}
        >
          {body}
        </div>
      ) : null}
    </div>

    <div style={{ ...footerBase, color: c.muted, borderTop: `2px solid ${c.rule}`, paddingTop: 28 }}>
      <span>paso {index}</span>
      <span>
        {index} / {total}
      </span>
    </div>
  </>
);

const CtaSlide: React.FC<{ heading?: string; body?: string; c: Colors }> = ({ heading, body, c }) => (
  <div
    style={{
      flex: 1,
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      textAlign: "center",
      gap: 40,
    }}
  >
    <div style={{ fontSize: 96, fontWeight: 700, lineHeight: 1.08, letterSpacing: -1 }}>{heading}</div>
    <div style={{ width: 168, height: 6, backgroundColor: c.ink }} />
    {body ? (
      <div style={{ fontFamily: SERIF, fontStyle: "italic", fontSize: 52, lineHeight: 1.42, color: c.quote }}>
        {body}
      </div>
    ) : null}
  </div>
);

const renderSlide = (slide: AnySlide, c: Colors): React.ReactNode => {
  if (slide.kind === "hero") {
    return <HeroSlide {...slide} c={c} />;
  }
  if (slide.kind === "cta") {
    return <CtaSlide heading={slide.heading} body={slide.body} c={c} />;
  }
  return <ItemSlide {...slide} c={c} />;
};

export const Carousel: React.FC<Props> = ({ slides, bgSrc, darkBg }) => {
  const frame = useCurrentFrame();
  const idx = Math.max(0, Math.min(frame, slides.length - 1));
  const slide = slides[idx];
  const onBg = !!bgSrc;
  const c: Colors = onBg && darkBg
    ? {
        ink: "#F3F1EC",
        muted: "rgba(243,241,236,0.72)",
        quote: "rgba(243,241,236,0.82)",
        rule: "rgba(243,241,236,0.24)",
        chipBg: "#F3F1EC",
        chipText: INK,
      }
    : LIGHT_COLORS;
  const style: React.CSSProperties = {
    ...frameStyleBase,
    backgroundColor: onBg ? "transparent" : BG,
    color: c.ink,
  };
  return (
    <AbsoluteFill>
      <Bg src={bgSrc} />
      <AbsoluteFill style={style}>{slide ? renderSlide(slide, c) : null}</AbsoluteFill>
    </AbsoluteFill>
  );
};

export default Carousel;
