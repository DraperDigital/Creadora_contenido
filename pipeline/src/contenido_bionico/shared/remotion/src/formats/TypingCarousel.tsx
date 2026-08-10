/**
 * TypingCarousel (1080x1920, opaque) — `videocarrusel_teclado` and
 * `videocarrusel_teclado_voz`.
 *
 * Each slide types its heading then its body character-by-character across its
 * scheduled typing window and holds through a dwell. Between slides the
 * operator motion language (`./motion`) takes over: the outgoing slide exits
 * with a fade + blur (no slide) and the incoming slide's elements enter from
 * their nearest edge (heading from the top, body from the bottom) with blur +
 * fade, decelerating into the settle. The entrance rides the schedule gap
 * (producer SLIDE_SEC), so typing starts exactly when the elements have
 * settled. The background is full-bleed and never moves; a block cursor
 * blinks while a slide is typing.
 *
 * Dark stage, light Poppins text, accent cursor. All type >= 46px, latin only.
 */
import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { loadBrandFonts } from "../lib/fonts";
import { DEFAULT_ENTER_SEC, DEFAULT_EXIT_SEC, enterStyle, exitStyle, type EnterFrom } from "./motion";

loadBrandFonts();

const FONT = "'Poppins', sans-serif";

type Palette = { bg: string; ink: string; paper: string; accent: string; accentSoft: string };
type Slide = { heading: string; body: string };
type Schedule = { start: number; typeEnd: number; end: number };
type TypingProps = { slides: Slide[]; schedule: Schedule[]; cursor: boolean; palette: Palette };

const clampBoth = { extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };

const Cursor: React.FC<{ p: Palette; show: boolean; size: number }> = ({ p, show, size }) => (
  <span
    style={{
      display: "inline-block",
      width: size * 0.58,
      height: size,
      marginLeft: size * 0.12,
      transform: "translateY(0.12em)",
      background: p.accent,
      opacity: show ? 1 : 0,
      borderRadius: 4,
    }}
  />
);

/** Element entrance wrapper (nearest-edge slide + blur + fade). */
const Enter: React.FC<{
  from: EnterFrom;
  at: number;
  dur: number;
  children?: React.ReactNode;
}> = ({ from, at, dur, children }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return <div style={enterStyle(frame, fps, { from, startSec: at, durSec: dur })}>{children}</div>;
};

const TypedSlide: React.FC<{
  p: Palette;
  slide: Slide;
  sc: Schedule;
  cursor: boolean;
  enterAt: number;
  enterDur: number;
}> = ({ p, slide, sc, cursor, enterAt, enterDur }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;

  const heading = slide.heading ?? "";
  const body = slide.body ?? "";
  const totalChars = heading.length + body.length;
  const revealed = Math.round(
    interpolate(t, [sc.start, sc.typeEnd], [0, totalChars], clampBoth),
  );
  const hChars = Math.max(0, Math.min(revealed, heading.length));
  const bChars = Math.max(0, Math.min(revealed - heading.length, body.length));

  const typingHeading = t >= sc.start && t < sc.typeEnd && revealed <= heading.length;
  const typingBody = t >= sc.start && t < sc.typeEnd && revealed > heading.length;
  // 2 Hz block-cursor blink while this slide is entering or actively typing.
  const blink = cursor && Math.floor(frame / (fps * 0.25)) % 2 === 0;

  return (
    <div style={{ width: "100%", padding: "0 108px", boxSizing: "border-box" }}>
      <Enter from="top" at={enterAt} dur={enterDur}>
        <div style={{ fontSize: 84, fontWeight: 800, lineHeight: 1.12, color: p.paper, letterSpacing: "-0.01em" }}>
          {heading.slice(0, hChars)}
          {typingHeading || t < sc.start ? <Cursor p={p} show={blink} size={72} /> : null}
        </div>
      </Enter>
      {body ? (
        <Enter from="bottom" at={enterAt} dur={enterDur}>
          <div style={{ marginTop: 44, fontSize: 54, fontWeight: 400, lineHeight: 1.4, color: p.paper, opacity: 0.86 }}>
            {body.slice(0, bChars)}
            {typingBody ? <Cursor p={p} show={blink} size={48} /> : null}
          </div>
        </Enter>
      ) : null}
    </div>
  );
};

export const TypingCarousel: React.FC<TypingProps> = ({ slides, schedule, cursor, palette }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;

  return (
    <AbsoluteFill style={{ backgroundColor: palette.bg, fontFamily: FONT }}>
      {slides.map((slide, k) => {
        const sc = schedule[k];
        if (!sc) return null;
        const isLast = k === slides.length - 1;
        // Entrance rides the schedule gap before this slide's window; the exit
        // (fade + blur, no slide) runs right after the window ends.
        const enterAt = k > 0 ? schedule[k - 1].end : Math.max(0, sc.start - DEFAULT_ENTER_SEC);
        const enterDur = Math.max(sc.start - enterAt, 0.01);
        if (t < enterAt) return null;
        if (!isLast && t >= sc.end + DEFAULT_EXIT_SEC) return null;
        const exit = !isLast && t >= sc.end ? exitStyle(frame, fps, { startSec: sc.end }) : undefined;
        return (
          <AbsoluteFill
            key={k}
            style={{ alignItems: "flex-start", justifyContent: "center", ...exit }}
          >
            <TypedSlide p={palette} slide={slide} sc={sc} cursor={cursor} enterAt={enterAt} enterDur={enterDur} />
          </AbsoluteFill>
        );
      })}
    </AbsoluteFill>
  );
};

export default TypingCarousel;
