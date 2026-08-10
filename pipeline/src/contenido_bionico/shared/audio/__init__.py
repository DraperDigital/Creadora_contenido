"""Audio layer for the animation pipeline.

Sits between assembly_render.py (which writes a video-only final.mp4) and the
end of the orchestrator. Mixes background music + per-segment sound effects
with the existing voice track at deterministic manifest-defined levels.

Modules:
    manifest          load audio_library/MANIFEST.json (sfx catalog + music config)
    validate_cues     parse + validate per-segment Sound_Cues.json
    build_audio_plan  gather Sound_Cues.json files into runs/<id>/Audio_Plan.json
    audio_mix         ffmpeg invocation that mixes music + sfx + voice over final.mp4
"""
