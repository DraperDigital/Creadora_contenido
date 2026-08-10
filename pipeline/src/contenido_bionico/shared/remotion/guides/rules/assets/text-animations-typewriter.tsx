import React from 'react';
import {
	AbsoluteFill,
	interpolate,
	useCurrentFrame,
	useVideoConfig,
} from 'remotion';

// Example asset for the guides. Target canvas: 1080x1920 (9:16).
// 'Poppins' is loaded globally by the pipeline — no font imports.
const COLOR_BG = '#0a0a0a';
const COLOR_TEXT = '#ffffff';
const FULL_TEXT = 'De la idea al video. Sin editar nada.';
const PAUSE_AFTER = 'De la idea al video.';
const FONT_SIZE = 88; // body minimum is 56px; headlines >= 120px
const FONT_WEIGHT = 600;
const CHAR_FRAMES = 2;
const CURSOR_BLINK_FRAMES = 16;
const PAUSE_SECONDS = 1;

const getTypedText = ({
	frame,
	fullText,
	pauseAfter,
	charFrames,
	pauseFrames,
}: {
	frame: number;
	fullText: string;
	pauseAfter: string;
	charFrames: number;
	pauseFrames: number;
}): string => {
	const pauseIndex = fullText.indexOf(pauseAfter);
	const preLen =
		pauseIndex >= 0 ? pauseIndex + pauseAfter.length : fullText.length;

	let typedChars = 0;
	if (frame < preLen * charFrames) {
		typedChars = Math.floor(frame / charFrames);
	} else if (frame < preLen * charFrames + pauseFrames) {
		typedChars = preLen;
	} else {
		const postPhase = frame - preLen * charFrames - pauseFrames;
		typedChars = Math.min(
			fullText.length,
			preLen + Math.floor(postPhase / charFrames),
		);
	}
	return fullText.slice(0, typedChars);
};

const Cursor: React.FC<{
	frame: number;
	blinkFrames: number;
	symbol?: string;
}> = ({frame, blinkFrames, symbol = '▌'}) => {
	const opacity = interpolate(
		frame % blinkFrames,
		[0, blinkFrames / 2, blinkFrames],
		[1, 0, 1],
		{extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
	);

	return <span style={{opacity}}>{symbol}</span>;
};

export const MyAnimation = () => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();

	const pauseFrames = Math.round(fps * PAUSE_SECONDS);

	const typedText = getTypedText({
		frame,
		fullText: FULL_TEXT,
		pauseAfter: PAUSE_AFTER,
		charFrames: CHAR_FRAMES,
		pauseFrames,
	});

	return (
		<AbsoluteFill
			style={{
				backgroundColor: COLOR_BG,
				justifyContent: 'center',
				padding: '0 80px 384px 80px', // keep text above the platform safe zone
			}}
		>
			<div
				style={{
					color: COLOR_TEXT,
					fontSize: FONT_SIZE,
					fontWeight: FONT_WEIGHT,
					fontFamily: "'Poppins', sans-serif",
					lineHeight: 1.2,
				}}
			>
				<span>{typedText}</span>
				<Cursor frame={frame} blinkFrames={CURSOR_BLINK_FRAMES} />
			</div>
		</AbsoluteFill>
	);
};
