/**
 * PosterLayout2 — element distribution only (no theme). A GIANT white headline
 * fills the TOP (behind the subject, so the head tucks just under it, face
 * clean); a signature-style italic line, a centered italic paragraph and a star
 * footer sit at the BOTTOM (in front, over the lower body). All text comes from
 * the shared neutral content — nothing themed is hardcoded. Solid or foto
 * background; a bottom scrim (front) keeps the copy readable over either.
 *
 * Adapts the visual arrangement of the old PosterAbout, rebuilt around the
 * generic `content`. Only Poppins is embedded, so the "signature/serif" feel is
 * faked with Poppins italic (synthetic oblique) + weight. Latin-1 text only.
 */
import React from "react";
import { AbsoluteFill } from "remotion";
import {
  POSTER_FONT,
  POSTER_W,
  Palette,
  PosterFrame,
  GiantStack,
  balanceHeadline,
  StarRow,
  fitToWidth,
  lightTextShadow,
} from "./posterKit";

const BG = "#121212";
const WHITE = "#FFFFFF";
const PARA_INK = "#ECEAE6";

// Headline typography — mirrored into the subject-tuck math below so the head
// always lands just under the last line (face clear of the type).
const H_WIDTH_PCT = 0.94;
const H_CONDENSE = 0.86;
const H_WEIGHT = "900";
const H_LINE_HEIGHT = 0.86;
const H_TRACKING = "-0.035em";
const H_TOP = 44;

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

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** A short quote reads as a bold signature line; a long one steps down so it
 * never blows past a couple of lines. Kept in the ~60-76px band for normal
 * lengths, easing to ~44px for the 120-char cap. */
const quoteSize = (len: number): number =>
  len <= 22 ? 74 : len <= 34 ? 66 : len <= 48 ? 58 : len <= 70 ? 50 : 44;

export const PosterLayout2: React.FC<Props> = ({ content, mode, subjectSrc, bgSrc, palette }) => {
  const accent = palette?.accent ?? WHITE;
  // Balance the headline into width-filling lines (no orphan function words) and
  // reuse THESE lines for both the subject-tuck sizing below and GiantStack, so
  // the two never disagree about where the first line sits.
  const lines = balanceHeadline((content.headline ?? []).filter(Boolean).join(" "), 3);
  const safe = lines.length ? lines : [" "];
  const quote = (content.quote ?? "").trim();
  const summary = (content.summary ?? "").trim();

  // Match GiantStack's own sizing to know the FIRST line's height, then tuck the
  // head just under that top line: the hair sits behind the type while the face
  // occludes the middle of the lower lines (type is `behind`, so it never
  // crosses the face — the layering rule keeps the face clean). Keeping the face
  // high also lands it ABOVE the bottom scrim, so the lit subject reads on the
  // near-black solid instead of sinking into it. (foto mode ignores this: the
  // plate + cutout share their own cover geometry.)
  const size = Math.min(
    ...safe.map((l) => fitToWidth(l, POSTER_W * H_WIDTH_PCT, H_WEIGHT, H_CONDENSE, H_TRACKING)),
  );
  const subjectTop = clamp(Math.round(H_TOP + size * H_LINE_HEIGHT * 0.6), 80, 300);

  const behind = (
    <GiantStack
      lines={lines}
      rebalance={false}
      color={WHITE}
      widthPct={H_WIDTH_PCT}
      condense={H_CONDENSE}
      weight={H_WEIGHT}
      lineHeight={H_LINE_HEIGHT}
      letterSpacing={H_TRACKING}
      align="center"
      style={{ position: "absolute", top: H_TOP, left: 0, right: 0 }}
    />
  );

  const front = (
    <>
      {/* Bottom scrim: fades the figure/plate into the dark so the copy always
          reads — over the near-black solid AND over a bright photo alike. */}
      <AbsoluteFill
        style={{
          background:
            "linear-gradient(to bottom, rgba(18,18,18,0) 42%, rgba(18,18,18,0.55) 55%, rgba(18,18,18,0.9) 72%, rgba(18,18,18,0.98) 100%)",
        }}
      />

      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 0,
          height: 660,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "flex-end",
          paddingBottom: 74,
        }}
      >
        {quote ? (
          <div
            style={{
              fontFamily: POSTER_FONT,
              fontStyle: "italic",
              fontWeight: 600,
              fontSize: quoteSize(quote.length),
              lineHeight: 1.05,
              letterSpacing: "-0.02em",
              color: WHITE,
              textShadow: lightTextShadow(WHITE, quoteSize(quote.length)),
              textAlign: "center",
              maxWidth: 900,
              padding: "0 44px",
            }}
          >
            {quote}
          </div>
        ) : null}

        {quote && summary ? (
          <div
            style={{
              width: 58,
              height: 2,
              backgroundColor: accent,
              opacity: 0.6,
              margin: "30px 0",
            }}
          />
        ) : (
          <div style={{ height: 34 }} />
        )}

        {summary ? (
          <div
            style={{
              fontFamily: POSTER_FONT,
              fontStyle: "italic",
              fontWeight: 400,
              fontSize: 32,
              lineHeight: 1.42,
              letterSpacing: "0.005em",
              color: PARA_INK,
              textShadow: lightTextShadow(PARA_INK),
              textAlign: "center",
              maxWidth: 800,
              padding: "0 44px",
            }}
          >
            {summary}
          </div>
        ) : null}

        <div style={{ marginTop: 46 }}>
          <StarRow color={WHITE} count={3} size={17} gap={26} />
        </div>
      </div>
    </>
  );

  return (
    <PosterFrame
      mode={mode}
      bgColor={BG}
      bgSrc={bgSrc}
      subjectSrc={subjectSrc}
      subjectTop={subjectTop}
      behind={behind}
      front={front}
      anim="pop"
    />
  );
};

export default PosterLayout2;
