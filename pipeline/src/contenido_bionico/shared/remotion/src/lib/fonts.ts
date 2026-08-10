// Deterministic local font loading for every render.
//
// 'Poppins' (weights 400 / 600 / 900) is the ONLY font family bundled with
// this tree. The OFL-licensed woff2 data (Google Fonts latin subset — covers
// Spanish: U+0000-00FF) is EMBEDDED as base64 data URLs in fontsData.ts and
// registered via injected CSS @font-face rules.
//
// WHY CSS + DATA URLS (do not "modernize" this back):
// - staticFile() fetches of the woff2 stalled indefinitely against the render
//   static server on this host, and
// - promise-based FontFace.load() + delayRender() froze in render pages under
//   Remotion's virtual-time frame capture (continuations never ran), so the
//   uncleared handles killed every render longer than ~28s — the 2026-07-10
//   captions incident (runs 30-32).
// CSS @font-face with an in-bundle data URL involves no network, no promises,
// and no delayRender: Chrome resolves the font during text layout, identically
// in every page. font-display: block guarantees no fallback-font frames.
//
// Call loadBrandFonts() once from any module in the render tree. Idempotent;
// can never fail or hang a render. @remotion/fonts and @remotion/google-fonts
// are NOT installed and must not be added.
import { POPPINS_400, POPPINS_600, POPPINS_900 } from "./fontsData";

export const BRAND_FONT_FAMILY = "Poppins";

const STYLE_ID = "bionico-brand-fonts";

const FACE_CSS = [
  ["400", POPPINS_400],
  ["600", POPPINS_600],
  ["900", POPPINS_900],
]
  .map(
    ([weight, data]) => `@font-face {
  font-family: '${BRAND_FONT_FAMILY}';
  font-style: normal;
  font-weight: ${weight};
  font-display: block;
  src: url(${data}) format('woff2');
}`,
  )
  .join("\n");

export const loadBrandFonts = (): void => {
  // Non-browser contexts (Node imports, type checks) have no document.
  if (typeof document === "undefined") {
    return;
  }
  if (document.getElementById(STYLE_ID) !== null) {
    return;
  }
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = FACE_CSS;
  (document.head ?? document.documentElement).appendChild(style);
};
