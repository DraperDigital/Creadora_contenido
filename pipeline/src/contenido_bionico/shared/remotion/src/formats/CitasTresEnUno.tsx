/**
 * CitasTresEnUno — up to three quotes stacked as branded cards in one
 * 1080x1350 still, with alternating left/right alignment and accent colors.
 *
 * Poppins only (Latin-1 subset): the decorative quote glyph is a guillemet
 * (« », within Latin-1), never a curly quote or emoji. Card text scales down
 * with length so long lines stay inside their card. Rendered at frame 0.
 */
import React from "react";
import { AbsoluteFill } from "remotion";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

const FONT = `'${BRAND_FONT_FAMILY}', sans-serif`;

type Palette = {
  bg: string;
  ink: string;
  paper: string;
  accent: string;
  accentSoft: string;
};

export type CitasTresEnUnoProps = {
  quotes: string[];
  palette: Palette;
};

const sizeFor = (t: string): number => {
  const n = (t ?? "").length;
  if (n > 120) return 34;
  if (n > 80) return 40;
  if (n > 45) return 48;
  return 56;
};

export const CitasTresEnUno: React.FC<CitasTresEnUnoProps> = ({ quotes, palette }) => {
  const items = (quotes ?? []).filter((q) => (q ?? "").trim()).slice(0, 3);
  return (
    <AbsoluteFill
      style={{
        backgroundColor: palette.bg,
        padding: "80px 74px",
        display: "flex",
        flexDirection: "column",
        gap: 30,
        fontFamily: FONT,
      }}
    >
      {items.map((quote, i) => {
        const right = i % 2 === 1;
        const accent = right ? palette.accentSoft : palette.accent;
        return (
          <div
            key={i}
            style={{
              flex: 1,
              backgroundColor: palette.paper,
              borderRadius: 40,
              padding: "48px 58px",
              display: "flex",
              flexDirection: "column",
              justifyContent: "center",
              alignItems: right ? "flex-end" : "flex-start",
              textAlign: right ? "right" : "left",
              overflow: "hidden",
              borderRight: right ? `12px solid ${accent}` : undefined,
              borderLeft: right ? undefined : `12px solid ${accent}`,
            }}
          >
            <div
              style={{
                fontWeight: 900,
                fontSize: 96,
                lineHeight: 0.6,
                color: accent,
                marginBottom: 8,
              }}
            >
              {right ? "»" : "«"}
            </div>
            <div
              style={{
                fontWeight: 800,
                fontSize: sizeFor(quote),
                lineHeight: 1.16,
                letterSpacing: "-0.01em",
                color: palette.ink,
                maxWidth: 860,
              }}
            >
              {quote}
            </div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

export default CitasTresEnUno;
