/**
 * PosterLayout3 — editorial distribution (no theme). A full-width MASTHEAD row +
 * a GIANT condensed headline fill the TOP (behind the subject); bordered benefit
 * call-outs, a right-margin barcode/edition/coords rail and a footer index sit at
 * the BOTTOM/margins (in front, over the body). Adapted from the old NIKE-style
 * PosterEditorial arrangement, rebuilt around the shared neutral `content`.
 *
 * Two modes, both legible:
 *  - solid: cream/paper page, near-black ink bare type.
 *  - foto:  a real photo plate. The bare giant type flips to light cream (with a
 *    soft shadow) so it reads over the photo; the bordered call-out / rail chips
 *    stay OPAQUE paper (dark ink on cream) so they punch out over any plate.
 * Only `content.*` supplies words; everything else is topic-less furniture.
 */
import React from "react";
import {
  POSTER_FONT,
  Palette,
  PosterFrame,
  GiantStack,
  Barcode,
  Crosshair,
  Micro,
} from "./posterKit";

const PAPER = "#EDE7DA"; // cream page
const INK = "#14110E"; // near-black ink
const CREAM = "#F4EFE4"; // light bare type for foto mode
const MARGIN = 56;
const BOX_W = 372;

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

/** A bordered call-out: bold small-caps heading + 1-2 line body, on a solid
 * paper chip — self-legible in BOTH modes (dark ink on cream over any plate). */
const CalloutBox: React.FC<{ heading: string; body: string; w?: number }> = ({
  heading,
  body,
  w = BOX_W,
}) => (
  <div
    style={{
      width: w,
      boxSizing: "border-box",
      border: `2px solid ${INK}`,
      backgroundColor: PAPER,
      padding: "15px 18px",
    }}
  >
    <div
      style={{
        fontFamily: POSTER_FONT,
        fontWeight: 700,
        fontSize: 23,
        letterSpacing: "0.04em",
        lineHeight: 1.02,
        textTransform: "uppercase",
        color: INK,
        marginBottom: body ? 8 : 0,
      }}
    >
      {heading}
    </div>
    {body ? (
      <div
        style={{
          fontFamily: POSTER_FONT,
          fontWeight: 500,
          fontSize: 19,
          lineHeight: 1.2,
          color: INK,
        }}
      >
        {body}
      </div>
    ) : null}
  </div>
);

export const PosterLayout3: React.FC<Props> = ({ content, mode, subjectSrc, bgSrc, palette }) => {
  const foto = mode === "foto";
  const accent = palette.accent;
  // Bare type (headline / masthead / footer / rule) flips to cream over a photo;
  // paper chips keep dark ink regardless. Micro + GiantStack self-apply the light-
  // text drop shadow when `bareInk` is light (foto mode) and skip it when dark
  // (solid ink on the cream page), so no per-element shadow prop is needed.
  const bareInk = foto ? CREAM : INK;

  const kw = (content.keywords ?? []).filter(Boolean);
  const mast = kw[0] || "EDICION";
  const headline = content.headline ?? [];
  const points = (content.points ?? []).filter((p) => p && p.heading).slice(0, 3);
  const leftBoxes = points.filter((_, i) => i !== 1); // [0] (+ [2] stacked)
  const rightBox = points[1];

  // --- TOP (behind the subject) ---
  const behind = (
    <>
      {/* Masthead row: mark + keyword | descriptor | vol + plus. */}
      <div
        style={{
          position: "absolute",
          top: 46,
          left: MARGIN,
          right: MARGIN,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ width: 14, height: 14, backgroundColor: accent }} />
          <Micro color={bareInk} size={20} tracking="0.2em" weight={700}>
            {mast}
          </Micro>
        </div>
        <Micro color={bareInk} size={18} tracking="0.26em" weight={600}>
          SERIE EDITORIAL
        </Micro>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Micro color={bareInk} size={18} tracking="0.2em" weight={600}>
            VOL 01
          </Micro>
          <div style={{ fontFamily: POSTER_FONT, fontWeight: 700, fontSize: 26, color: accent, lineHeight: 1 }}>
            +
          </div>
        </div>
      </div>
      {/* Rule under the masthead. */}
      <div
        style={{
          position: "absolute",
          top: 88,
          left: MARGIN,
          right: MARGIN,
          height: 2,
          backgroundColor: bareInk,
          ...(foto ? { boxShadow: "0 2px 14px rgba(0,0,0,0.5)" } : {}),
        }}
      />

      {/* Giant headline — the hero, behind the subject. */}
      <GiantStack
        lines={headline}
        color={bareInk}
        widthPct={0.93}
        condense={0.8}
        weight="900"
        lineHeight={0.86}
        letterSpacing="-0.035em"
        style={{ position: "absolute", top: 150, left: 40, right: 40 }}
      />
    </>
  );

  // --- BOTTOM / margins (in front, over the body) ---
  const front = (
    <>
      {/* Lower-left call-out stack (points 0 and 2), bottom-anchored. */}
      {leftBoxes.length ? (
        <div
          style={{
            position: "absolute",
            left: MARGIN,
            bottom: 160,
            display: "flex",
            flexDirection: "column",
            gap: 16,
          }}
        >
          {leftBoxes.map((b, i) => (
            <CalloutBox key={i} heading={b.heading} body={b.body} />
          ))}
        </div>
      ) : null}

      {/* Lower-right call-out (point 1). */}
      {rightBox ? (
        <div style={{ position: "absolute", right: MARGIN, bottom: 160 }}>
          <CalloutBox heading={rightBox.heading} body={rightBox.body} />
        </div>
      ) : null}

      {/* Right-margin furniture (upper): vertical barcode, edition box, coords.
       * Opaque paper chips so they punch cleanly out of type/subject/photo. */}
      <div
        style={{
          position: "absolute",
          right: 40,
          top: 208,
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          gap: 22,
        }}
      >
        <div
          style={{
            width: 60,
            height: 150,
            border: `2px solid ${INK}`,
            backgroundColor: PAPER,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Barcode color={INK} w={132} h={40} style={{ transform: "rotate(90deg)" }} />
        </div>
        <div style={{ border: `2px solid ${INK}`, backgroundColor: PAPER, padding: "6px 10px" }}>
          <Micro color={INK} size={17} tracking="0.14em" weight={700}>
            ED. 25-01
          </Micro>
        </div>
        <div style={{ border: `2px solid ${INK}`, backgroundColor: PAPER, padding: "12px 7px" }}>
          <div
            style={{
              fontFamily: POSTER_FONT,
              fontWeight: 600,
              fontSize: 16,
              letterSpacing: "0.22em",
              color: INK,
              textTransform: "uppercase",
              writingMode: "vertical-rl",
            }}
          >
            LAT 19.4 LON 99.1
          </div>
        </div>
      </div>

      {/* Footer bar: 3-part index | crosshair | reference. */}
      <div
        style={{
          position: "absolute",
          left: MARGIN,
          right: MARGIN,
          bottom: 54,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Micro color={bareInk} size={17} tracking="0.16em" weight={600}>
          001&nbsp;&nbsp;/&nbsp;&nbsp;SERIE&nbsp;&nbsp;/&nbsp;&nbsp;003
        </Micro>
        <Crosshair color={accent} size={26} />
        <Micro color={bareInk} size={17} tracking="0.16em" weight={600}>
          REF 25-01
        </Micro>
      </div>
    </>
  );

  return (
    <PosterFrame
      mode={mode}
      bgColor={PAPER}
      bgSrc={bgSrc}
      subjectSrc={subjectSrc}
      subjectTop={156}
      behind={behind}
      front={front}
      anim="wipe"
    />
  );
};

export default PosterLayout3;
