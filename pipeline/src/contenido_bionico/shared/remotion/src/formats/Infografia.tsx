/**
 * Infografia — the video's key points as a single 1080x1350 summary still,
 * in three layout organizations (the operator picks one):
 *
 *   variant 1 — vertical numbered list.
 *   variant 2 — two-column grid of cards (an odd last card spans both columns).
 *   variant 3 — centered title + vertical timeline of steps.
 *
 * OVERFLOW-PROOF BY CONSTRUCTION: the producer feeds content bounded by the
 * structural caps (title <= 48 chars, eyebrow <= 24, 3-5 points of heading
 * <= 36 / body <= 90 — see images.py / agents/infografia.md) and every layout
 * here holds even at max stress: the point area is a fixed flex/grid region
 * (`flex: 1; minHeight: 0`), rows/cards share it equally (`flex: 1 1 0` /
 * `minmax(0, 1fr)`), font sizes scale down with point count, and every text
 * run carries a hard line-clamp with `overflow: hidden`. Nothing can touch a
 * canvas edge; worst case a text run ellipsizes inside its own box.
 *
 * Poppins only (Latin-1 subset), so no emoji and no non-Latin punctuation
 * (…, arrows). Rendered at frame 0.
 */
import React from "react";
import { AbsoluteFill } from "remotion";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

const WIDTH = 1080;
const HEIGHT = 1350;
const FONT = `'${BRAND_FONT_FAMILY}', sans-serif`;
const MAX_POINTS = 5;
const BODY_MAX = 90;

type Palette = {
  bg: string;
  ink: string;
  paper: string;
  accent: string;
  accentSoft: string;
};

type Point = { heading: string; body: string };

export type InfografiaProps = {
  variant: 1 | 2 | 3;
  title: string;
  eyebrow: string;
  points: Point[];
  palette: Palette;
};

const clip = (s: string, n = BODY_MAX): string => {
  const t = (s ?? "").trim();
  return t.length > n ? `${t.slice(0, n).trimEnd()}...` : t;
};

/** Hard line-clamp: at most `n` lines, ellipsized, never overflowing its box. */
const clampLines = (n: number): React.CSSProperties => ({
  display: "-webkit-box",
  WebkitBoxOrient: "vertical",
  WebkitLineClamp: n,
  overflow: "hidden",
});

/** Title font size: scales with length so even a 48-char title fits 2 lines. */
const titleSize = (title: string, base: number): number => {
  const n = (title ?? "").length;
  if (n > 36) return base - 20;
  if (n > 24) return base - 10;
  return base;
};

const Eyebrow: React.FC<{ text: string; color: string }> = ({ text, color }) => {
  const clean = (text ?? "").trim();
  if (!clean) return null;
  return (
    <div
      style={{
        fontFamily: FONT,
        fontWeight: 700,
        fontSize: 30,
        letterSpacing: "0.22em",
        paddingLeft: "0.22em",
        textTransform: "uppercase",
        whiteSpace: "nowrap",
        overflow: "hidden",
        color,
      }}
    >
      {clean}
    </div>
  );
};

const Header: React.FC<{
  title: string;
  eyebrow: string;
  eyebrowColor: string;
  base: number;
  centered?: boolean;
  marginBottom: number;
}> = ({ title, eyebrow, eyebrowColor, base, centered, marginBottom }) => (
  <div
    style={{
      display: "flex",
      flexDirection: "column",
      gap: 12,
      marginBottom,
      flexShrink: 0,
      alignItems: centered ? "center" : "flex-start",
      textAlign: centered ? "center" : "left",
    }}
  >
    <Eyebrow text={eyebrow} color={eyebrowColor} />
    <div
      style={{
        fontWeight: 900,
        fontSize: titleSize(title, base),
        lineHeight: 1.06,
        letterSpacing: "-0.02em",
        ...clampLines(2),
      }}
    >
      {title}
    </div>
  </div>
);

/** variant 1: vertical numbered list. */
const ListLayout: React.FC<InfografiaProps> = ({ title, eyebrow, points, palette }) => {
  const n = points.length;
  const badge = n >= 5 ? 60 : n === 4 ? 68 : 76;
  const badgeFont = n >= 5 ? 32 : n === 4 ? 36 : 42;
  const headingFont = n >= 5 ? 34 : n === 4 ? 38 : 42;
  const bodyFont = n >= 5 ? 27 : n === 4 ? 29 : 31;
  return (
    <AbsoluteFill
      style={{
        backgroundColor: palette.paper,
        padding: "100px 96px",
        display: "flex",
        flexDirection: "column",
        fontFamily: FONT,
        color: palette.ink,
      }}
    >
      <Header
        title={title}
        eyebrow={eyebrow}
        eyebrowColor={palette.accent}
        base={76}
        marginBottom={44}
      />
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {points.map((p, i) => (
          <div
            key={i}
            style={{
              flex: "1 1 0",
              minHeight: 0,
              display: "flex",
              alignItems: "center",
              gap: 32,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                flexShrink: 0,
                width: badge,
                height: badge,
                borderRadius: 18,
                backgroundColor: palette.accent,
                color: palette.paper,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontWeight: 900,
                fontSize: badgeFont,
              }}
            >
              {i + 1}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
              <div
                style={{ fontWeight: 800, fontSize: headingFont, lineHeight: 1.12, ...clampLines(2) }}
              >
                {p.heading}
              </div>
              {p.body ? (
                <div
                  style={{
                    fontWeight: 400,
                    fontSize: bodyFont,
                    lineHeight: 1.28,
                    opacity: 0.72,
                    ...clampLines(2),
                  }}
                >
                  {clip(p.body)}
                </div>
              ) : null}
            </div>
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};

/** variant 2: two-column grid of cards; an odd last card spans the full row. */
const GridLayout: React.FC<InfografiaProps> = ({ title, eyebrow, points, palette }) => {
  const n = points.length;
  const rows = Math.max(1, Math.ceil(n / 2));
  const numberFont = n >= 5 ? 32 : 38;
  const headingFont = n >= 5 ? 30 : 34;
  const bodyFont = n >= 5 ? 25 : 28;
  const cardPad = n >= 5 ? "24px 28px" : "28px 32px";
  return (
    <AbsoluteFill
      style={{
        backgroundColor: palette.bg,
        padding: "96px 84px",
        display: "flex",
        flexDirection: "column",
        fontFamily: FONT,
        color: palette.paper,
      }}
    >
      <Header
        title={title}
        eyebrow={eyebrow}
        eyebrowColor={palette.accentSoft}
        base={70}
        marginBottom={40}
      />
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
          gap: 22,
          overflow: "hidden",
        }}
      >
        {points.map((p, i) => {
          const spansRow = n % 2 === 1 && i === n - 1;
          return (
            <div
              key={i}
              style={{
                gridColumn: spansRow ? "1 / -1" : undefined,
                minHeight: 0,
                backgroundColor: "rgba(255,255,255,0.06)",
                border: `2px solid ${palette.accent}`,
                borderRadius: 28,
                padding: cardPad,
                display: "flex",
                flexDirection: "column",
                gap: 8,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  fontWeight: 900,
                  fontSize: numberFont,
                  color: palette.accent,
                  lineHeight: 1,
                  flexShrink: 0,
                }}
              >
                {String(i + 1).padStart(2, "0")}
              </div>
              <div
                style={{ fontWeight: 800, fontSize: headingFont, lineHeight: 1.14, ...clampLines(2) }}
              >
                {p.heading}
              </div>
              {p.body ? (
                <div
                  style={{
                    fontWeight: 400,
                    fontSize: bodyFont,
                    lineHeight: 1.26,
                    opacity: 0.82,
                    ...clampLines(3),
                  }}
                >
                  {clip(p.body)}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** variant 3: centered title + vertical timeline of steps. */
const TimelineLayout: React.FC<InfografiaProps> = ({ title, eyebrow, points, palette }) => {
  const n = points.length;
  const circle = n >= 5 ? 56 : n === 4 ? 60 : 64;
  const circleFont = n >= 5 ? 30 : n === 4 ? 32 : 34;
  const headingFont = n >= 5 ? 34 : n === 4 ? 36 : 38;
  const bodyFont = n >= 5 ? 27 : n === 4 ? 28 : 30;
  return (
    <AbsoluteFill
      style={{
        backgroundColor: palette.paper,
        padding: "92px 90px",
        display: "flex",
        flexDirection: "column",
        fontFamily: FONT,
        color: palette.ink,
      }}
    >
      <Header
        title={title}
        eyebrow={eyebrow}
        eyebrowColor={palette.accent}
        base={68}
        centered
        marginBottom={36}
      />
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {points.map((p, i) => {
          const last = i === points.length - 1;
          return (
            <div
              key={i}
              style={{
                flex: "1 1 0",
                minHeight: 0,
                display: "flex",
                alignItems: "stretch",
                gap: 30,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  width: circle,
                  flexShrink: 0,
                }}
              >
                <div
                  style={{
                    width: circle,
                    height: circle,
                    borderRadius: 999,
                    backgroundColor: palette.accent,
                    color: palette.paper,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontWeight: 900,
                    fontSize: circleFont,
                    flexShrink: 0,
                  }}
                >
                  {i + 1}
                </div>
                {!last ? (
                  <div
                    style={{
                      flex: 1,
                      width: 4,
                      backgroundColor: palette.accentSoft,
                      marginTop: 6,
                      marginBottom: 6,
                    }}
                  />
                ) : null}
              </div>
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 6,
                  paddingTop: 2,
                  minWidth: 0,
                }}
              >
                <div
                  style={{ fontWeight: 800, fontSize: headingFont, lineHeight: 1.12, ...clampLines(2) }}
                >
                  {p.heading}
                </div>
                {p.body ? (
                  <div
                    style={{
                      fontWeight: 400,
                      fontSize: bodyFont,
                      lineHeight: 1.26,
                      opacity: 0.72,
                      ...clampLines(2),
                    }}
                  >
                    {clip(p.body)}
                  </div>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

export const Infografia: React.FC<InfografiaProps> = (props) => {
  const points = (props.points ?? []).slice(0, MAX_POINTS);
  const p = { ...props, points };
  switch (props.variant) {
    case 2:
      return <GridLayout {...p} />;
    case 3:
      return <TimelineLayout {...p} />;
    case 1:
    default:
      return <ListLayout {...p} />;
  }
};

export default Infografia;
