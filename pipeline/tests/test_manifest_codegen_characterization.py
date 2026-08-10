"""Characterization test for the production manifest-codegen path.

Pins the behavior of `write_compositions_ts` / `copy_scene_tsx` (scene import
lines, Captions imports). Task 3 made the Captions import CONDITIONAL
on a per-run copy, so this test pins BOTH cases:

  (a) shared fallback (`./Captions`) when NO per-run copy exists —
      the production/from-scratch path, which must stay byte-for-byte as before;
  (b) per-run import (`./runs/<run>/Captions`) when a per-run copy is
      present in the run dir.

The scene-import assertion is unchanged. These pins let later refactor tasks
prove they did not alter the shared fallback nor the opt-in per-run override.
"""
from pathlib import Path
from contenido_bionico.shared.remotion_manifest_short import (
    copy_scene_tsx, write_compositions_ts,
)


def test_scene_import_and_shared_captions(remotion_src_sandbox, tmp_path):
    src = tmp_path / "Scene.tsx"
    src.write_text("export default () => null;\n", encoding="utf-8")
    copy_scene_tsx(run_id=42, segment_id=1, source_scene_tsx=src)
    out = remotion_src_sandbox / "Compositions.generated.ts"
    write_compositions_ts(
        run_id=42,
        scenes=[{"segment_id": 1, "duration_sec": 5.0, "default_props": {}}],
        captions_props={"durationSec": 5.0, "cues": [], "placement": "center", "brand": {"font": "X"}},
        out_path=out,
    )
    ts = out.read_text(encoding="utf-8")
    # Case (a): baseline shared fallback — production emits no per-run copy.
    assert 'import Scene_42_1 from "./runs/42/1/Scene";' in ts
    assert 'import { Captions } from "./Captions";' in ts   # shared fallback still valid
    assert (remotion_src_sandbox / "runs" / "42" / "1" / "Scene.tsx").exists()


def test_per_run_copy_switches_captions_import(remotion_src_sandbox, tmp_path):
    """Case (b): when the run dir holds a per-run Captions.tsx copy,
    the manifest imports it instead of the shared component (scene import
    unchanged)."""
    src = tmp_path / "Scene.tsx"
    src.write_text("export default () => null;\n", encoding="utf-8")
    copy_scene_tsx(run_id=42, segment_id=1, source_scene_tsx=src)

    run_dir = tmp_path / "runs" / "42"
    (run_dir / "remotion").mkdir(parents=True)
    (run_dir / "remotion" / "Captions.tsx").write_text(
        'import type { CaptionsProps } from "./lib/types";\n'
        "export const Captions = () => null;\nexport default Captions;\n",
        encoding="utf-8",
    )

    out = remotion_src_sandbox / "Compositions.generated.ts"
    write_compositions_ts(
        run_id=42,
        scenes=[{"segment_id": 1, "duration_sec": 5.0, "default_props": {}}],
        captions_props={"durationSec": 5.0, "cues": [], "placement": "center", "brand": {"font": "X"}},
        out_path=out,
        run_dir=run_dir,
    )
    ts = out.read_text(encoding="utf-8")
    assert 'import Scene_42_1 from "./runs/42/1/Scene";' in ts  # scene import intact
    assert 'import { Captions } from "./runs/42/Captions";' in ts
    assert 'from "./Captions";' not in ts
