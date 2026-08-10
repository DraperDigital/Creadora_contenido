/**
 * WhatsappCarousel (1080x1920, opaque) — `videocarrusel_whatsapp`.
 *
 * A GENERIC two-sided chat mock (no WhatsApp logo or trademarked assets)
 * playing an agent-written conversation about the video's content. Right-side
 * ("usuario") messages TYPE character-by-character across their scheduled
 * window and send at `typeEnd`; left-side ("otro") messages show a three-dot
 * typing indicator from `start` and pop in at `typeEnd`. Per the operator
 * motion language (`./motion`) every bubble enters from ITS OWN side — right
 * column from the right, left column from the left — with blur + fade and a
 * decelerating settle; the indicator gets the same entrance and the background
 * is full-bleed and never moves. Timestamps are small and grey; read-ticks on
 * sent messages use the brand accent. System sans is intentional — it reads
 * as a phone UI.
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { enterStyle } from "./motion";

const SANS = '"Segoe UI", "Helvetica Neue", system-ui, Arial, sans-serif';

type Palette = { bg: string; ink: string; paper: string; accent: string; accentSoft: string };
type ChatEvent = { de: "usuario" | "otro"; texto: string; start: number; typeEnd: number };
type WhatsappProps = { chat: ChatEvent[]; palette: Palette; name?: string; avatarSrc?: string };

const clampBoth = { extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };

const OUT_BUBBLE = "#DCF8C6"; // classic outgoing light-green (generic)
const OUT_INK = "#0B1F14";
const STAMP = "#5A6B5F";
const HEADER_H = 150;
const BUBBLE_TRAVEL = 120; // entrance travel for bubbles (px)

// A stable pseudo clock label per message index (chat cosmetics only).
const stamp = (i: number): string => {
  const base = 9 * 60 + 41 + i * 2; // minutes past midnight, +2 per message
  const hh = Math.floor(base / 60) % 24;
  const mm = base % 60;
  return `${hh}:${String(mm).padStart(2, "0")}`;
};

const Ticks: React.FC<{ color: string }> = ({ color }) => (
  <svg width="34" height="20" viewBox="0 0 34 20" style={{ marginLeft: 8 }}>
    <g fill="none" stroke={color} strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 11l5 5L18 5" />
      <path d="M13 16L24 5" />
    </g>
  </svg>
);

const Caret: React.FC<{ show: boolean }> = ({ show }) => (
  <span
    style={{
      display: "inline-block",
      width: 5,
      height: 44,
      marginLeft: 6,
      transform: "translateY(0.14em)",
      background: OUT_INK,
      opacity: show ? 0.85 : 0,
      borderRadius: 2,
    }}
  />
);

/** Right-side ("usuario") bubble: enters from the right, text types in. */
const OutgoingBubble: React.FC<{ ev: ChatEvent; i: number; accent: string }> = ({ ev, i, accent }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;
  const revealed = Math.round(interpolate(t, [ev.start, ev.typeEnd], [0, ev.texto.length], clampBoth));
  const typing = t < ev.typeEnd;
  const blink = Math.floor(frame / (fps * 0.25)) % 2 === 0;
  const sent = t >= ev.typeEnd;
  return (
    <div
      style={{
        alignSelf: "flex-end",
        maxWidth: 800,
        background: OUT_BUBBLE,
        color: OUT_INK,
        borderRadius: 34,
        borderTopRightRadius: 10,
        padding: "28px 34px 22px",
        marginTop: 26,
        boxShadow: "0 10px 26px rgba(0,0,0,0.30)",
        ...enterStyle(frame, fps, { from: "right", startSec: ev.start, travelPx: BUBBLE_TRAVEL }),
      }}
    >
      <div style={{ fontSize: 46, fontWeight: 400, lineHeight: 1.34 }}>
        {ev.texto.slice(0, revealed)}
        {typing ? <Caret show={blink} /> : null}
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "flex-end",
          marginTop: 8,
          opacity: sent ? 1 : 0.4,
        }}
      >
        <span style={{ fontSize: 26, color: STAMP }}>{stamp(i)}</span>
        {sent ? <Ticks color={accent} /> : null}
      </div>
    </div>
  );
};

/** Left-side ("otro") bubble: arrives at typeEnd, entering from the left. */
const IncomingBubble: React.FC<{ ev: ChatEvent; i: number; palette: Palette }> = ({ ev, i, palette }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <div
      style={{
        alignSelf: "flex-start",
        maxWidth: 800,
        background: palette.paper,
        color: palette.ink,
        borderRadius: 34,
        borderTopLeftRadius: 10,
        padding: "28px 34px 22px",
        marginTop: 26,
        boxShadow: "0 10px 26px rgba(0,0,0,0.30)",
        ...enterStyle(frame, fps, { from: "left", startSec: ev.typeEnd, travelPx: BUBBLE_TRAVEL }),
      }}
    >
      <div style={{ fontSize: 46, fontWeight: 400, lineHeight: 1.34 }}>{ev.texto}</div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
        <span style={{ fontSize: 26, color: STAMP }}>{stamp(i)}</span>
      </div>
    </div>
  );
};

/** Left-side three-dot typing indicator shown while an "otro" message is due. */
const TypingIndicator: React.FC<{ at: number; palette: Palette }> = ({ at, palette }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <div
      style={{
        alignSelf: "flex-start",
        background: palette.paper,
        borderRadius: 34,
        borderTopLeftRadius: 10,
        padding: "30px 34px",
        marginTop: 26,
        display: "flex",
        gap: 16,
        boxShadow: "0 10px 26px rgba(0,0,0,0.30)",
        ...enterStyle(frame, fps, { from: "left", startSec: at, travelPx: BUBBLE_TRAVEL }),
      }}
    >
      {[0, 1, 2].map((d) => {
        const o = interpolate((frame + d * 6) % 30, [0, 15, 30], [0.3, 1, 0.3], clampBoth);
        return <span key={d} style={{ width: 22, height: 22, borderRadius: 11, background: palette.accent, opacity: o }} />;
      })}
    </div>
  );
};

export const WhatsappCarousel: React.FC<WhatsappProps> = ({ chat, palette, name, avatarSrc }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;

  const title = (name || "").trim() || "Chat";
  const avatarLetter = title.charAt(0).toUpperCase() || "C";

  return (
    <AbsoluteFill style={{ backgroundColor: palette.bg, fontFamily: SANS }}>
      {/* generic chat header (no trademarked marks); enters once from the top */}
      <div
        style={{
          height: HEADER_H,
          display: "flex",
          alignItems: "center",
          gap: 28,
          padding: "0 48px",
          background: "rgba(255,255,255,0.05)",
          borderBottom: "1px solid rgba(255,255,255,0.08)",
          ...enterStyle(frame, fps, { from: "top", startSec: 0, travelPx: 80 }),
        }}
      >
        {avatarSrc ? (
          <Img
            src={staticFile(avatarSrc)}
            style={{ width: 92, height: 92, borderRadius: "50%", objectFit: "cover" }}
          />
        ) : (
          <div
            style={{
              width: 92,
              height: 92,
              borderRadius: "50%",
              background: palette.accent,
              color: palette.paper,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 46,
              fontWeight: 700,
            }}
          >
            {avatarLetter}
          </div>
        )}
        <div style={{ display: "flex", flexDirection: "column" }}>
          <span style={{ fontSize: 42, fontWeight: 600, color: palette.paper }}>{title}</span>
          <span style={{ fontSize: 28, color: palette.accentSoft }}>en línea</span>
        </div>
      </div>

      {/* message stack, anchored to the bottom safe zone */}
      <AbsoluteFill
        style={{
          top: HEADER_H,
          bottom: 0,
          padding: "0 48px 430px",
          display: "flex",
          flexDirection: "column",
          justifyContent: "flex-end",
          overflow: "hidden",
        }}
      >
        {chat.map((ev, i) => {
          if (t < ev.start) return null;
          if (ev.de === "usuario") {
            return <OutgoingBubble key={i} ev={ev} i={i} accent={palette.accent} />;
          }
          if (t < ev.typeEnd) {
            return <TypingIndicator key={i} at={ev.start} palette={palette} />;
          }
          return <IncomingBubble key={i} ev={ev} i={i} palette={palette} />;
        })}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export default WhatsappCarousel;
