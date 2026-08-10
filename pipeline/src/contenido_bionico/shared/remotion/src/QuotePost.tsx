/**
 * Quote post (tweet layout), 1080x1350 — ONE quote per render, to a lossless PNG.
 *
 * Three stacked layers (z-order = JSX order):
 *   1. template  the 1122x1402 quote_layout.png (photo, name, verified badge,
 *                handle, AND its baked-in lorem ipsum) scaled to fill 1080x1350.
 *   2. mask      a flat #f8f8f8 rectangle (the exact card background) from
 *                top:400 down — it paints over the template's placeholder text.
 *   3. quote     the real quote, bold dark ink, in an 896px-wide column at
 *                left:92 / top:438, top-aligned.
 *
 * The body renders in a system sans
 * so NO bundled font file is required — only the brand template image, staged by
 * build_quotes into public/assets/quote/layout.png. The line wrapping that Python
 * pre-computed (build_quotes._fit_quote_font_px, heuristic) matches a bold sans.
 */
import React from "react";
import { AbsoluteFill, Img, staticFile } from "remotion";
import type { QuotePostProps } from "./lib/types";

const WIDTH = 1080;
const HEIGHT = 1350;
// System sans stack (no bundled font). Matches the modern tweet-card look.
const FONT_FAMILY =
  '-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
const CARD_BG = "#f8f8f8"; // exact card background, sampled from the PNG
const INK = "#0f1419";

export const QuotePost: React.FC<QuotePostProps> = ({ text, fontPx }) => {
  return (
    <AbsoluteFill>
      {/* Layer 1: the template, scaled to fill the 1080x1350 frame. */}
      <Img
        src={staticFile("assets/quote/layout.png")}
        style={{ position: "absolute", left: 0, top: 0, width: WIDTH, height: HEIGHT }}
      />
      {/* Layer 2: erase the template's placeholder body text. */}
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          top: 400,
          bottom: 0,
          backgroundColor: CARD_BG,
        }}
      />
      {/* Layer 3: the real quote, top-aligned in an 896px column. */}
      <div
        style={{
          position: "absolute",
          left: 92,
          right: 92,
          top: 438,
          fontFamily: FONT_FAMILY,
          fontWeight: 800,
          fontSize: fontPx,
          color: INK,
          lineHeight: 1.16,
          letterSpacing: -1,
          textAlign: "left",
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

export default QuotePost;
