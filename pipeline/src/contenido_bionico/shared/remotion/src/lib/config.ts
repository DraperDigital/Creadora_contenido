import { loadBrandFonts } from "./fonts";

// Brand fonts must be registered for EVERY render, including the per-render
// isolated entrypoints that register only a single authored Scene (they import
// this module for FPS and nothing else from the shared tree — see
// shared/render_remotion.py). Loading here — idempotent, never render-blocking
// on failure — guarantees fontFamily 'Poppins' resolves in every composition.
loadBrandFonts();

// Single TS-side source of truth for the render frame rate.
//
// Every composition (scenes/segments, captions) renders at this rate.
// The Python codegen mirrors it as DEFAULT_FPS in
// shared/remotion_manifest_writer.py — keep the two in sync.
export const FPS = 30;

// Canvas dimensions (vertical 9:16).
export const CANVAS_W = 1080;
export const CANVAS_H = 1920;

// GLOBAL FORBIDDEN ZONE: the bottom 20% of the frame is reserved for the
// platform's own UI (video description, follow button, profile picture, etc.).
// No content — captions, the comment card, scene visuals/text — may sit there.
// Everything must live in the top 80% (y = 0 .. CONTENT_BOTTOM_Y).
export const FORBIDDEN_BOTTOM_RATIO = 0.2;
export const CONTENT_BOTTOM_Y = CANVAS_H * (1 - FORBIDDEN_BOTTOM_RATIO); // 1536
