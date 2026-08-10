/**
 * PosterLayout4 — element distribution only (no theme). A GIANT tall-condensed
 * stacked headline fills the frame at the TOP (behind the subject), its lines
 * alternating SOLID / OUTLINE white so the type reads as a magazine backdrop the
 * person stands in front of; up to 3 short benefit lines + a decorative footer
 * sit at the BOTTOM (in front, over the body). All text comes from the shared
 * neutral content. Solid (sky gradient) or foto (real plate) background.
 *
 * The headline is the hero: to read as ONE block every line shares a single size
 * (the min that fits the widest line) — this mirrors GiantStack's fit-the-widest
 * sizing but varies the FILL per line (solid, outline, solid), which the shared
 * primitive's all-or-nothing `outline` prop cannot do.
 */
import React from "react";
import {
  POSTER_W,
  POSTER_FONT,
  Palette,
  PosterFrame,
  Micro,
  Crosshair,
  fitToWidth,
  balanceHeadline,
  lightTextShadow,
} from "./posterKit";

const WHITE = "#FFFFFF";

// Bright vertical sky gradient (stands in for a bright sky photo in solid mode):
// sky blue up top, fading to a paler blue-white lower down.
const SKY_TOP = "#A9C7DE";
const SKY_MID = "#C6DAE8";
const SKY_LOW = "#DCE8F0";
const BG = "#C3D7E6"; // matching flat fallback

// Tall-condensed headline geometry (condense ~0.74 fakes narrow condensed type).
const CONDENSE = 0.74;
const WIDTH_PCT = 0.98;
const LINE_HEIGHT = 0.84;
const TRACKING = "-0.04em";
const WEIGHT = "900";
const STROKE = 4;

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

/** The hero headline: one shared size across every line (so it reads as ONE
 * headline), each line SOLID or OUTLINE white by index — solid, outline, solid.
 * Mirrors GiantStack's fit-the-widest sizing but varies the fill per line. */
const MixedGiantStack: React.FC<{ lines: string[] }> = ({ lines }) => {
  // Re-flow into balanced, width-filling lines (no orphan function words) before
  // the alternating solid/outline treatment.
  const reflowed = balanceHeadline(lines.join(" "));
  const clean = reflowed.map((l) => l.toUpperCase()).filter(Boolean);
  const safe = clean.length ? clean : [" "];
  const target = POSTER_W * WIDTH_PCT;
  const size = Math.min(
    ...safe.map((l) => fitToWidth(l, target, WEIGHT, CONDENSE, TRACKING)),
  );
  return (
    <div
      style={{
        position: "absolute",
        top: 58,
        left: 0,
        right: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
      }}
    >
      {safe.map((line, i) => {
        const outline = i % 2 === 1; // solid, OUTLINE, solid, ...
        return (
          <div
            key={i}
            style={{
              fontFamily: POSTER_FONT,
              fontWeight: WEIGHT,
              fontSize: size,
              lineHeight: LINE_HEIGHT,
              letterSpacing: TRACKING,
              whiteSpace: "nowrap",
              transform: `scaleX(${CONDENSE})`,
              transformOrigin: "center",
              color: outline ? "transparent" : WHITE,
              WebkitTextStroke: outline ? `${STROKE}px ${WHITE}` : undefined,
              // Light headline: the tight 80% drop shadow in BOTH modes (over the
              // sky gradient AND a photo). Outline lines keep just the stroke.
              textShadow: outline ? undefined : lightTextShadow(WHITE, size),
            }}
          >
            {line}
          </div>
        );
      })}
    </div>
  );
};

export const PosterLayout4: React.FC<Props> = ({ content, mode, subjectSrc, bgSrc, palette }) => {
  const accent = palette.accent;
  const headline = content.headline ?? [];
  const benefits = (content.points ?? []).map((p) => p.heading).filter(Boolean).slice(0, 3);

  // Solid-mode designed background (ignored in foto mode — the real plate wins).
  const bgNode = (
    <div
      style={{
        position: "absolute",
        inset: 0,
        background: `linear-gradient(180deg, ${SKY_TOP} 0%, ${SKY_MID} 52%, ${SKY_LOW} 100%)`,
      }}
    />
  );

  // TOP → behind the subject: the giant tall-condensed mixed headline.
  const behind = <MixedGiantStack lines={headline} />;

  // BOTTOM → in front, over the body: benefit lines + a decorative footer.
  const front = (
    <>
      {benefits.length ? (
        <div style={{ position: "absolute", left: 56, top: 1046 }}>
          <div style={{ width: 46, height: 5, backgroundColor: accent, marginBottom: 18 }} />
          {benefits.map((line, i) => (
            <Micro
              key={i}
              color={WHITE}
              size={23}
              tracking="0.16em"
              weight={600}
              style={{ marginBottom: 12 }}
            >
              {line}
            </Micro>
          ))}
        </div>
      ) : null}

      {/* Decorative footer — a thin rule + a small registration mark. No text. */}
      <div
        style={{
          position: "absolute",
          left: 54,
          right: 54,
          bottom: 92,
          height: 2,
          backgroundColor: WHITE,
          opacity: 0.85,
        }}
      />
      <Crosshair
        color={accent}
        size={28}
        style={{ position: "absolute", left: POSTER_W / 2 - 14, bottom: 42 }}
      />
    </>
  );

  return (
    <PosterFrame
      mode={mode}
      bgColor={BG}
      bgNode={bgNode}
      bgSrc={bgSrc}
      subjectSrc={subjectSrc}
      subjectTop={150}
      behind={behind}
      front={front}
      anim="rise"
    />
  );
};

export default PosterLayout4;
