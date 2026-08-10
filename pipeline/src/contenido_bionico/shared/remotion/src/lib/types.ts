// Shared Remotion composition types for every format (long, short, faceless).
// One generic Root renders all three from a single generated manifest, so each
// composition entry carries explicit width/height.

export type WordTiming = {
  word: string;
  start: number;
  end: number;
};

export type CaptionCue = {
  text: string;
  start: number;
  end: number;
};

export type CaptionsProps = {
  durationSec: number;
  cues: CaptionCue[];
  placement: "center" | "bottom";
  // The single caption face: every word renders in this brand font.
  brand: { font: string };
  // Word fill color as a CSS/hex string. Optional: absent or JSON null (the
  // Python codegen emits an explicit null when unset) -> the committed white
  // default (STANDARD_COLOR in Captions.tsx). Set per-run via a caption color
  // edit (e.g. "subtitle color yellow").
  color?: string | null;
  fontPx?: number; // fixed caption size for this format (long is smaller than short)
  // SHORT split-screen only: when present and the active cue time is within
  // [start, end), the (center-placed) cue block anchors just ABOVE
  // lineYRatio*height (the divider line). Absent/null keeps the exact current
  // placement, so long/faceless render byte-identically.
  split?: { start: number; end: number; lineYRatio: number } | null;
};

export type SceneProps = {
  segmentId: string;
  durationSec: number;
  words: WordTiming[];
};

// --- Carousel (4:5 PNG slides) ------------------------------------------------
// A static, neutral-palette "key points" carousel built from a finished short.
// Each slide is one frame of the `carousel` composition, rendered to a PNG
// still. The hero slide opens with the topic; every other slide is one key
// point with a numbered chip, a heading and a short body line. All text is
// precomputed in Python so the component stays a dumb renderer.

export type CarouselHeroSlide = {
  kind: "hero";
  eyebrow: string; // small letter-spaced kicker, e.g. "Guia rapida"
  title: string; // the topic / hook of the short
  footnote?: string; // e.g. "5 claves"
};

export type CarouselItemSlide = {
  kind: "item";
  index: number; // 1-based step number (shown in the chip)
  total: number; // total key-point count
  heading: string; // the key idea (short)
  body?: string; // one-line takeaway in the creator's words; omitted when none
};

export type CarouselSlide = CarouselHeroSlide | CarouselItemSlide;

export type CarouselProps = {
  slides: CarouselSlide[];
};

// --- Quote posts (10s tweet-card mp4s) ----------------------------------------
// The brand "tweet" card on the committed `assets/quote/layout.png` template (pfp,
// name, handle, verified badge baked in); only the body TEXT changes per quote,
// rendered live in a system sans (no bundled font file). ONE quote per render:
// the `quote-post` composition is a single frame rendered to a lossless PNG,
// which ffmpeg loops into a 10s mp4 with music. The font size is fitted in Python
// (heuristic) so the quote fills the text box.

export type QuotePostProps = {
  text: string; // the quote, paraphrased from the creator's own words
  fontPx: number; // body font size, fitted in Python to the text box
};

export type CompositionEntry = {
  id: string; // `segment-${N}`, "captions", "carousel", or "quote-post"
  durationInFrames: number; // round(durationSec * fps)
  width: number;
  height: number;
  defaultProps:
    | Record<string, never>
    | SceneProps
    | CaptionsProps
    | CarouselProps
    | QuotePostProps;
};
