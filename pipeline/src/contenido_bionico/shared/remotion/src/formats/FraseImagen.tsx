/**
 * FraseImagen — one strong quote as a 1080x1350 still, in three brand styles.
 * Rendered at frame 0 (a still); no animation, pure layout.
 *
 *   solida    — accent-filled canvas, giant inverted (bg-colored) hero text.
 *   foto      — a user photo, cover-filled, darkened, white hero text low.
 *   plantilla — branded paper card: accent corner shapes, eyebrow chip, footer.
 *
 * Poppins is registered by loadBrandFonts() (also globally via lib/config, but
 * this call keeps the component self-sufficient). fitText autoshrinks the hero
 * text to the column; the result is clamped so short quotes never explode and
 * long ones stay readable. Latin-1 text only (no emoji).
 */
import React from "react";
import { AbsoluteFill, Img, staticFile } from "remotion";
import { fitText } from "@remotion/layout-utils";
import { BRAND_FONT_FAMILY, loadBrandFonts } from "../lib/fonts";

loadBrandFonts();

const WIDTH = 1080;
const HEIGHT = 1350;
const FONT = `'${BRAND_FONT_FAMILY}', sans-serif`;

type Palette = {
  bg: string;
  ink: string;
  paper: string;
  accent: string;
  accentSoft: string;
};

type FraseStyle = "solida" | "foto" | "plantilla";

export type FraseImagenProps = {
  style: FraseStyle;
  text: string;
  attribution: string;
  photoSrc?: string;
  /** Chip text on the "plantilla" style (default "FRASE"; quote cards pass "CITA"). */
  eyebrow?: string;
  palette: Palette;
};

/** Largest Poppins-900 size that fits `text` on one line in `withinWidth`,
 * clamped to [min, max]. The rendered block wraps, so this drives the hero
 * scale (short quote -> big, long quote -> floor) without overflowing. */
const fitHero = (
  text: string,
  withinWidth: number,
  max: number,
  min: number
): number => {
  const { fontSize } = fitText({
    text: text || " ",
    withinWidth,
    fontFamily: BRAND_FONT_FAMILY,
    fontWeight: "900",
  });
  return Math.max(min, Math.min(max, fontSize));
};

const Attribution: React.FC<{ text: string; color: string }> = ({ text, color }) => {
  const clean = (text ?? "").trim();
  if (!clean) return null;
  return (
    <div
      style={{
        fontFamily: FONT,
        fontWeight: 600,
        fontSize: 34,
        letterSpacing: "0.02em",
        color,
        opacity: 0.7,
        marginTop: 40,
      }}
    >
      {clean}
    </div>
  );
};

const Solida: React.FC<FraseImagenProps> = ({ text, attribution, palette }) => {
  const size = fitHero(text, 920, 150, 54);
  return (
    <AbsoluteFill
      style={{
        backgroundColor: palette.accent,
        padding: "120px 90px",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
      }}
    >
      <div
        style={{
          fontFamily: FONT,
          fontWeight: 900,
          fontSize: size,
          lineHeight: 1.06,
          letterSpacing: "-0.02em",
          color: palette.bg,
          maxWidth: 920,
        }}
      >
        {text}
      </div>
      <Attribution text={attribution} color={palette.bg} />
    </AbsoluteFill>
  );
};

// Hero-over-photo: the b-roll is a DARKENED full-bleed backdrop (half opacity
// over near-black + a soft vignette) and the phrase is set BIG and CENTERED on
// top — the poster treatment, no cutout. Used by cita_foto and frase_foto.
const FOTO_DARK_BASE = "#0D0D0F";
const Foto: React.FC<FraseImagenProps> = ({ text, attribution, photoSrc, palette }) => {
  const size = fitHero(text, 940, 150, 58);
  return (
    <AbsoluteFill style={{ backgroundColor: FOTO_DARK_BASE }}>
      {photoSrc ? (
        <Img
          src={staticFile(photoSrc)}
          style={{ width: WIDTH, height: HEIGHT, objectFit: "cover", opacity: 0.5 }}
        />
      ) : null}
      {/* Soft centered vignette so the middle text stays crisp over any photo. */}
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(115% 85% at 50% 50%, rgba(0,0,0,0.12) 0%, rgba(0,0,0,0.5) 100%)",
        }}
      />
      <AbsoluteFill
        style={{
          padding: "0 76px",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          textAlign: "center",
          gap: 34,
        }}
      >
        <div
          style={{
            fontFamily: FONT,
            fontWeight: 900,
            fontSize: size,
            lineHeight: 1.06,
            letterSpacing: "-0.02em",
            color: "#ffffff",
            maxWidth: 968,
            textShadow: "0 6px 30px rgba(0,0,0,0.5)",
          }}
        >
          {text}
        </div>
        <Attribution text={attribution} color="#ffffff" />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Plantilla: React.FC<FraseImagenProps> = ({ text, attribution, eyebrow, palette }) => {
  const size = fitHero(text, 860, 110, 46);
  const chip = (eyebrow ?? "").trim() || "FRASE";
  return (
    <AbsoluteFill style={{ backgroundColor: palette.paper }}>
      {/* Accent corner shapes. */}
      <svg
        width={WIDTH}
        height={HEIGHT}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        style={{ position: "absolute", inset: 0 }}
      >
        <polygon points="0,0 300,0 0,300" fill={palette.accent} />
        <polygon
          points={`${WIDTH},${HEIGHT} ${WIDTH - 300},${HEIGHT} ${WIDTH},${HEIGHT - 300}`}
          fill={palette.accentSoft}
        />
        <circle cx={WIDTH - 150} cy={150} r={26} fill={palette.accent} />
      </svg>
      <AbsoluteFill
        style={{
          padding: "150px 110px",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          textAlign: "center",
        }}
      >
        {/* Eyebrow chip. */}
        <div
          style={{
            fontFamily: FONT,
            fontWeight: 700,
            fontSize: 30,
            letterSpacing: "0.28em",
            paddingLeft: "0.28em",
            color: palette.paper,
            backgroundColor: palette.accent,
            borderRadius: 999,
            padding: "14px 34px",
            marginBottom: 56,
          }}
        >
          {chip}
        </div>
        <div
          style={{
            fontFamily: FONT,
            fontWeight: 900,
            fontSize: size,
            lineHeight: 1.1,
            letterSpacing: "-0.015em",
            color: palette.ink,
            maxWidth: 860,
          }}
        >
          {text}
        </div>
        <Attribution text={attribution} color={palette.ink} />
      </AbsoluteFill>
      {/* Footer brand line. */}
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 70,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 20,
        }}
      >
        <div style={{ width: 120, height: 4, borderRadius: 2, backgroundColor: palette.accent }} />
        <div style={{ width: 14, height: 14, borderRadius: 999, backgroundColor: palette.accent }} />
        <div style={{ width: 120, height: 4, borderRadius: 2, backgroundColor: palette.accent }} />
      </div>
    </AbsoluteFill>
  );
};

export const FraseImagen: React.FC<FraseImagenProps> = (props) => {
  switch (props.style) {
    case "foto":
      return <Foto {...props} />;
    case "plantilla":
      return <Plantilla {...props} />;
    case "solida":
    default:
      return <Solida {...props} />;
  }
};

export default FraseImagen;
