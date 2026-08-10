"""Tests for Task 3 — per-run editable COPY of Captions.tsx.

These exercise the pure Python seams only (copy, import-depth rewrite, and the
conditional import emitted by `write_compositions_ts`). No real Remotion render,
no agents — the sandbox `remotion_src_sandbox` fixture points REMOTION_SRC at a
tmp dir so nothing touches the real `shared/remotion/src/runs/`.

Backward-compat invariant: a run with NO per-run copy must emit the SHARED
import (`./Captions`) exactly as today; the per-run import is opt-in
and only taken when `runs/<run>/remotion/<component>.tsx` exists.
"""
from pathlib import Path

import pytest

from contenido_bionico.shared.remotion_manifest_short import write_compositions_ts
from contenido_bionico.shared import remotion_manifest_writer as writer


CAPTIONS_PROPS = {
    "durationSec": 1.0,
    "cues": [],
    "placement": "center",
    "brand": {"font": "X"},
}


def _seed_shared(sandbox: Path) -> None:
    """Write a minimal shared component source into the sandbox so the copy
    helpers (which read from REMOTION_SRC) have something to read."""
    (sandbox / "Captions.tsx").write_text(
        'import type { CaptionsProps } from "./lib/types";\n'
        "export const Captions = () => null;\nexport default Captions;\n",
        encoding="utf-8",
    )


# --- no per-run copy: shared import fallback (backward-compat) ----------------


def test_no_copy_uses_shared_captions_import(remotion_src_sandbox, tmp_path):
    _seed_shared(remotion_src_sandbox)
    out = remotion_src_sandbox / "Compositions.generated.ts"
    write_compositions_ts(
        run_id=7,
        captions_props=CAPTIONS_PROPS,
        out_path=out,
        run_dir=tmp_path / "runs" / "7",  # exists-less: no per-run copy
    )
    ts = out.read_text("utf-8")
    assert 'import { Captions } from "./Captions";' in ts
    assert 'from "./runs/7/Captions"' not in ts


def test_run_dir_none_preserves_shared_imports(remotion_src_sandbox):
    """Existing callers that never pass run_dir keep today's shared imports."""
    _seed_shared(remotion_src_sandbox)
    out = remotion_src_sandbox / "Compositions.generated.ts"
    write_compositions_ts(
        run_id=7,
        captions_props=CAPTIONS_PROPS,
        out_path=out,
    )
    ts = out.read_text("utf-8")
    assert 'import { Captions } from "./Captions";' in ts


# --- per-run copy present: per-run import + staged copy -----------------------


def test_per_run_captions_copy_overrides_import(remotion_src_sandbox, tmp_path):
    _seed_shared(remotion_src_sandbox)
    run_dir = tmp_path / "runs" / "7"
    (run_dir / "remotion").mkdir(parents=True)
    (run_dir / "remotion" / "Captions.tsx").write_text(
        'import type { CaptionsProps } from "./lib/types";\n'
        "export const Captions = () => null; // PATCHED\n"
        "export default Captions;\n",
        encoding="utf-8",
    )
    out = remotion_src_sandbox / "Compositions.generated.ts"
    write_compositions_ts(
        run_id=7,
        captions_props=CAPTIONS_PROPS,
        out_path=out,
        run_dir=run_dir,
    )
    ts = out.read_text("utf-8")
    assert 'from "./runs/7/Captions"' in ts
    assert 'from "./Captions";' not in ts
    staged = remotion_src_sandbox / "runs" / "7" / "Captions.tsx"
    assert staged.exists()
    assert "PATCHED" in staged.read_text("utf-8")


# --- import-depth rewrite (the gotcha) ---------------------------------------


def test_staged_copy_rewrites_relative_import_depth(remotion_src_sandbox, tmp_path):
    """The staged copy sits at REMOTION_SRC/runs/<run>/<component>.tsx — one dir
    deeper than the shared component (which sits at REMOTION_SRC/). Its
    `./lib/types` import must be rewritten to `../../lib/types` so it still
    resolves from the deeper location."""
    _seed_shared(remotion_src_sandbox)
    run_dir = tmp_path / "runs" / "7"
    (run_dir / "remotion").mkdir(parents=True)
    (run_dir / "remotion" / "Captions.tsx").write_text(
        'import type { CaptionsProps } from "./lib/types";\n'
        "export const Captions = () => null;\nexport default Captions;\n",
        encoding="utf-8",
    )
    out = remotion_src_sandbox / "Compositions.generated.ts"
    write_compositions_ts(
        run_id=7,
        captions_props=CAPTIONS_PROPS,
        out_path=out,
        run_dir=run_dir,
    )
    staged = (remotion_src_sandbox / "runs" / "7" / "Captions.tsx").read_text("utf-8")
    assert '"../../lib/types"' in staged
    assert '"./lib/types"' not in staged


def test_stage_run_component_returns_none_without_copy(remotion_src_sandbox, tmp_path):
    _seed_shared(remotion_src_sandbox)
    run_dir = tmp_path / "runs" / "7"
    run_dir.mkdir(parents=True)  # no remotion/ subdir
    assert (
        writer.stage_run_component(run_id=7, component="Captions", run_dir=run_dir)
        is None
    )


def test_stage_run_component_stages_and_returns_path(remotion_src_sandbox, tmp_path):
    _seed_shared(remotion_src_sandbox)
    run_dir = tmp_path / "runs" / "7"
    (run_dir / "remotion").mkdir(parents=True)
    (run_dir / "remotion" / "Captions.tsx").write_text(
        'import type { CaptionsProps } from "./lib/types";\n'
        "export const Captions = () => null;\nexport default Captions;\n",
        encoding="utf-8",
    )
    staged = writer.stage_run_component(run_id=7, component="Captions", run_dir=run_dir)
    assert staged is not None
    assert staged == remotion_src_sandbox / "runs" / "7" / "Captions.tsx"
    assert staged.exists()


def test_copy_shared_component_seeds_run_dir(remotion_src_sandbox, tmp_path):
    """copy_shared_component_tsx seeds runs/<run>/remotion/<component>.tsx from
    the committed shared source so the change agent can then Edit that copy."""
    _seed_shared(remotion_src_sandbox)
    run_dir = tmp_path / "runs" / "7"
    run_dir.mkdir(parents=True)
    dest = writer.copy_shared_component_tsx(
        run_id=7, component="Captions", run_dir=run_dir
    )
    assert dest == run_dir / "remotion" / "Captions.tsx"
    assert dest.exists()
    assert "Captions" in dest.read_text("utf-8")
