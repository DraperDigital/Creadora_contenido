import React from 'react';
import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';

/*
 * Highlight a word in a sentence with a spring-animated wipe effect.
 * Example asset for the guides. Target canvas: 1080x1920 (9:16).
 * 'Poppins' is loaded globally by the pipeline — no font imports.
 */

const COLOR_BG = '#0a0a0a';
const COLOR_TEXT = '#ffffff';
const COLOR_HIGHLIGHT = '#D4AF37';
const FULL_TEXT = 'Esto es lo importante.';
const HIGHLIGHT_WORD = 'importante';
const FONT_SIZE = 88; // body minimum is 56px
const FONT_WEIGHT = 600;
const HIGHLIGHT_START_FRAME = 30;
const HIGHLIGHT_WIPE_DURATION = 18;

const Highlight: React.FC<{
	word: string;
	color: string;
	delay: number;
	durationInFrames: number;
}> = ({word, color, delay, durationInFrames}) => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();

	// spring-snappy preset: no bounce, confident settle.
	const highlightProgress = spring({
		fps,
		frame,
		config: {damping: 200},
		delay,
		durationInFrames,
	});
	const scaleX = Math.max(0, Math.min(1, highlightProgress));

	return (
		<span style={{position: 'relative', display: 'inline-block'}}>
			<span
				style={{
					position: 'absolute',
					left: 0,
					right: 0,
					top: '50%',
					height: '1.05em',
					transform: `translateY(-50%) scaleX(${scaleX})`,
					transformOrigin: 'left center',
					backgroundColor: color,
					borderRadius: '0.18em',
					zIndex: 0,
				}}
			/>
			<span style={{position: 'relative', zIndex: 1}}>{word}</span>
		</span>
	);
};

export const MyAnimation = () => {
	const highlightIndex = FULL_TEXT.indexOf(HIGHLIGHT_WORD);
	const hasHighlight = highlightIndex >= 0;
	const preText = hasHighlight ? FULL_TEXT.slice(0, highlightIndex) : FULL_TEXT;
	const postText = hasHighlight
		? FULL_TEXT.slice(highlightIndex + HIGHLIGHT_WORD.length)
		: '';

	return (
		<AbsoluteFill
			style={{
				backgroundColor: COLOR_BG,
				alignItems: 'center',
				justifyContent: 'center',
				padding: '0 80px 384px 80px', // keep text above the platform safe zone
				fontFamily: "'Poppins', sans-serif",
			}}
		>
			<div
				style={{
					color: COLOR_TEXT,
					fontSize: FONT_SIZE,
					fontWeight: FONT_WEIGHT,
					textAlign: 'center',
				}}
			>
				{hasHighlight ? (
					<>
						<span>{preText}</span>
						<Highlight
							word={HIGHLIGHT_WORD}
							color={COLOR_HIGHLIGHT}
							delay={HIGHLIGHT_START_FRAME}
							durationInFrames={HIGHLIGHT_WIPE_DURATION}
						/>
						<span>{postText}</span>
					</>
				) : (
					<span>{FULL_TEXT}</span>
				)}
			</div>
		</AbsoluteFill>
	);
};
