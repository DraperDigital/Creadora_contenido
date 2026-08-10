/**
 * SlideshowCarousel (1080x1920, opaque) — the video-carousel backbone for
 * `videocarrusel_voz_1` (card), `videocarrusel_voz_2` (editorial) and
 * `videocarrusel_musica` (card, timed).
 *
 * A hero title slide followed by one slide per key point. Each slide owns a
 * window on the timeline (cumulative `durationsSec`, hero = slide 0). The
 * background is FULL-BLEED and persistent; only elements move (operator motion
 * spec, `./motion`): at a slide's window start its elements slide in from
 * their nearest layout edge with blur + fade (expo ease-out, small stagger),
 * and at the window end the whole slide exits with a fade + blur — the canvas
 * itself never sweeps.
 *
 * Poppins via loadBrandFonts() (data-URL @font-face, never network/delayRender).
 * All type >= 44px, vertically centred, latin-only (no emoji).
 */
import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { loadBrandFonts } from "../lib/fonts";
import { DEFAULT_EXIT_SEC, enterStyle, exitStyle, type EnterFrom } from "./motion";

loadBrandFonts();

const FONT = "'Poppins', sans-serif";
const STAGGER_SEC = 0.07; // per-element entrance offset inside a slide

type Palette = {
  bg: string;
  ink: string;
  paper: string;
  accent: string;
  accentSoft: string;
};

type ItemSlide = { heading: string; body: string; index: number; total: number };

type SlideshowProps = {
  styleVariant: "card" | "editorial";
  title: string;
  eyebrow: string;
  slides: ItemSlide[];
  durationsSec: number[]; // per slide incl. hero as index 0
  palette: Palette;
};

/** Element entrance wrapper: slides in from `from` at `at` (+stagger order). */
const Enter: React.FC<{
  from: EnterFrom;
  at: number;
  order?: number;
  style?: React.CSSProperties;
  children?: React.ReactNode;
}> = ({ from, at, order = 0, style, children }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <div style={{ ...style, ...enterStyle(frame, fps, { from, startSec: at + order * STAGGER_SEC }) }}>
      {children}
    </div>
  );
};

const Hero: React.FC<{ p: Palette; variant: string; title: string; eyebrow: string; at: number }> = ({
  p,
  variant,
  title,
  eyebrow,
  at,
}) =>
  variant === "editorial" ? (
    <div style={{ width: "100%", padding: "0 96px", boxSizing: "border-box" }}>
      <Enter from="top" at={at} order={0}>
        <div
          style={{
            fontSize: 46,
            fontWeight: 600,
            letterSpacing: "0.32em",
            textTransform: "uppercase",
            color: p.accent,
            marginBottom: 40,
          }}
        >
          {eyebrow}
        </div>
      </Enter>
      <Enter from="left" at={at} order={1}>
        <div style={{ height: 8, width: 220, background: p.accent, marginBottom: 56 }} />
      </Enter>
      <Enter from="left" at={at} order={2}>
        <div
          style={{
            fontSize: 128,
            fontWeight: 900,
            lineHeight: 1.02,
            letterSpacing: "-0.02em",
            color: p.ink,
          }}
        >
          {title}
        </div>
      </Enter>
    </div>
  ) : (
    <Enter
      from="bottom"
      at={at}
      order={0}
      style={{
        width: 860,
        background: p.paper,
        borderRadius: 56,
        padding: "96px 72px",
        boxShadow: "0 40px 90px rgba(0,0,0,0.28)",
        textAlign: "center",
        boxSizing: "border-box",
      }}
    >
      <Enter from="top" at={at} order={1}>
        <div
          style={{
            fontSize: 44,
            fontWeight: 600,
            letterSpacing: "0.3em",
            textTransform: "uppercase",
            color: p.accent,
            marginBottom: 44,
          }}
        >
          {eyebrow}
        </div>
      </Enter>
      <Enter from="bottom" at={at} order={2}>
        <div
          style={{
            fontSize: 104,
            fontWeight: 900,
            lineHeight: 1.05,
            letterSpacing: "-0.015em",
            color: p.ink,
          }}
        >
          {title}
        </div>
      </Enter>
    </Enter>
  );

const Item: React.FC<{ p: Palette; variant: string; s: ItemSlide; at: number }> = ({ p, variant, s, at }) =>
  variant === "editorial" ? (
    <div style={{ width: "100%", padding: "0 96px", boxSizing: "border-box" }}>
      <Enter from="top" at={at} order={0}>
        <div style={{ fontSize: 200, fontWeight: 900, lineHeight: 1, color: p.accentSoft }}>
          {String(s.index).padStart(2, "0")}
        </div>
      </Enter>
      <Enter from="left" at={at} order={1}>
        <div style={{ height: 6, width: "100%", background: p.ink, opacity: 0.14, margin: "36px 0 48px" }} />
      </Enter>
      <Enter from="left" at={at} order={2}>
        <div style={{ fontSize: 92, fontWeight: 900, lineHeight: 1.05, color: p.ink, marginBottom: 40 }}>
          {s.heading}
        </div>
      </Enter>
      {s.body ? (
        <Enter from="bottom" at={at} order={3}>
          <div style={{ fontSize: 52, fontWeight: 400, lineHeight: 1.42, color: p.ink, opacity: 0.82 }}>
            {s.body}
          </div>
        </Enter>
      ) : null}
    </div>
  ) : (
    <Enter
      from="bottom"
      at={at}
      order={0}
      style={{
        width: 860,
        background: p.paper,
        borderRadius: 56,
        padding: "84px 72px",
        boxShadow: "0 40px 90px rgba(0,0,0,0.28)",
        boxSizing: "border-box",
      }}
    >
      <Enter from="left" at={at} order={1}>
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 132,
            height: 132,
            borderRadius: 30,
            background: p.accent,
            color: p.paper,
            fontSize: 76,
            fontWeight: 900,
            marginBottom: 48,
          }}
        >
          {s.index}
        </div>
      </Enter>
      <Enter from="top" at={at} order={2}>
        <div style={{ fontSize: 78, fontWeight: 900, lineHeight: 1.08, color: p.ink, marginBottom: 36 }}>
          {s.heading}
        </div>
      </Enter>
      {s.body ? (
        <Enter from="bottom" at={at} order={3}>
          <div style={{ fontSize: 50, fontWeight: 400, lineHeight: 1.4, color: p.ink, opacity: 0.8 }}>
            {s.body}
          </div>
        </Enter>
      ) : null}
    </Enter>
  );

export const SlideshowCarousel: React.FC<SlideshowProps> = ({
  styleVariant,
  title,
  eyebrow,
  slides,
  durationsSec,
  palette,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;

  const bg = styleVariant === "editorial" ? palette.paper : palette.bg;

  // Unified slide list: hero (index 0) + item slides.
  const all: ("hero" | ItemSlide)[] = ["hero", ...slides];

  // Cumulative window starts.
  const starts: number[] = [];
  let acc = 0;
  for (let i = 0; i < all.length; i++) {
    starts.push(acc);
    acc += durationsSec[i] ?? 0;
  }

  return (
    <AbsoluteFill style={{ backgroundColor: bg, fontFamily: FONT }}>
      {all.map((slide, k) => {
        const start = starts[k];
        const end = start + (durationsSec[k] ?? 0);
        const isLast = k === all.length - 1;
        // Elements enter at the window start; the slide exits (fade + blur)
        // finishing exactly at the window end. Outside that, render nothing.
        if (t < start) return null;
        if (!isLast && t >= end) return null;
        const exit = !isLast && t >= end - DEFAULT_EXIT_SEC
          ? exitStyle(frame, fps, { startSec: end - DEFAULT_EXIT_SEC })
          : undefined;
        return (
          <AbsoluteFill
            key={k}
            style={{ alignItems: "center", justifyContent: "center", ...exit }}
          >
            {slide === "hero" ? (
              <Hero p={palette} variant={styleVariant} title={title} eyebrow={eyebrow} at={start} />
            ) : (
              <Item p={palette} variant={styleVariant} s={slide} at={start} />
            )}
          </AbsoluteFill>
        );
      })}
    </AbsoluteFill>
  );
};

export default SlideshowCarousel;
