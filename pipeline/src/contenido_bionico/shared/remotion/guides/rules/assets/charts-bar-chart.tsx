import React from 'react';
import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';

// Example asset for the guides. Target canvas: 1080x1920 (9:16).
// 'Poppins' is loaded globally by the pipeline — no font imports.
// In a real scene, take these colors from STYLE_TOKENS_JSON.palette.
const COLOR_BAR = '#D4AF37';
const COLOR_TEXT = '#ffffff';
const COLOR_MUTED = '#9a9a9a';
const COLOR_BG = '#0a0a0a';
const COLOR_AXIS = '#333333';

const FONT_FAMILY = "'Poppins', sans-serif";

const Title: React.FC<{children: React.ReactNode}> = ({children}) => (
	<div style={{textAlign: 'center', marginBottom: 60}}>
		<div style={{color: COLOR_TEXT, fontSize: 120, fontWeight: 900, lineHeight: 1.05}}>
			{children}
		</div>
	</div>
);

const YAxis: React.FC<{steps: number[]; height: number}> = ({
	steps,
	height,
}) => (
	<div
		style={{
			display: 'flex',
			flexDirection: 'column',
			justifyContent: 'space-between',
			height,
			paddingRight: 20,
		}}
	>
		{steps
			.slice()
			.reverse()
			.map((step) => (
				<div
					key={step}
					style={{
						color: COLOR_MUTED,
						fontSize: 36,
						fontWeight: 400,
						textAlign: 'right',
					}}
				>
					{step.toLocaleString()}
				</div>
			))}
	</div>
);

const Bar: React.FC<{
	height: number;
	progress: number;
}> = ({height, progress}) => (
	<div
		style={{
			flex: 1,
			display: 'flex',
			flexDirection: 'column',
			justifyContent: 'flex-end',
		}}
	>
		<div
			style={{
				width: '100%',
				height,
				backgroundColor: COLOR_BAR,
				borderRadius: '10px 10px 0 0',
				opacity: progress,
			}}
		/>
	</div>
);

const XAxis: React.FC<{
	children: React.ReactNode;
	labels: string[];
	height: number;
}> = ({children, labels, height}) => (
	<div style={{flex: 1, display: 'flex', flexDirection: 'column'}}>
		<div
			style={{
				display: 'flex',
				alignItems: 'flex-end',
				gap: 20,
				height,
				borderLeft: `3px solid ${COLOR_AXIS}`,
				borderBottom: `3px solid ${COLOR_AXIS}`,
				paddingLeft: 20,
			}}
		>
			{children}
		</div>
		<div
			style={{
				display: 'flex',
				gap: 20,
				paddingLeft: 20,
				marginTop: 16,
			}}
		>
			{labels.map((label) => (
				<div
					key={label}
					style={{
						flex: 1,
						textAlign: 'center',
						color: COLOR_MUTED,
						fontSize: 36,
						fontWeight: 600,
					}}
				>
					{label}
				</div>
			))}
		</div>
	</div>
);

export const MyAnimation = () => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();

	const data = [
		{month: 'Ene', price: 2039},
		{month: 'Mar', price: 2160},
		{month: 'May', price: 2327},
		{month: 'Jul', price: 2426},
		{month: 'Sep', price: 2634},
		{month: 'Nov', price: 2672},
	];

	const minPrice = 2000;
	const maxPrice = 2800;
	const priceRange = maxPrice - minPrice;
	// Keep the chart inside the focal zone, above the platform safe zone (y=1536).
	const chartHeight = 800;
	const yAxisSteps = [2000, 2400, 2800];

	return (
		<AbsoluteFill
			style={{
				backgroundColor: COLOR_BG,
				padding: '260px 80px 500px 80px',
				display: 'flex',
				flexDirection: 'column',
				fontFamily: FONT_FAMILY,
			}}
		>
			<Title>Precio del oro</Title>

			<div style={{display: 'flex', flex: 1}}>
				<YAxis steps={yAxisSteps} height={chartHeight} />
				<XAxis height={chartHeight} labels={data.map((d) => d.month)}>
					{data.map((item, i) => {
						// spring-soft preset: organic growth, staggered 5 frames apart.
						const progress = spring({
							frame: frame - i * 5 - 10,
							fps,
							config: {damping: 18, stiffness: 80},
						});

						const barHeight =
							((item.price - minPrice) / priceRange) * chartHeight * progress;

						return (
							<Bar key={item.month} height={barHeight} progress={progress} />
						);
					})}
				</XAxis>
			</div>
		</AbsoluteFill>
	);
};
