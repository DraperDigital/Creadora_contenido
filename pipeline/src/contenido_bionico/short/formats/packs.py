"""Per-category deliverable ZIPs (`pack_<category>.zip`).

One ZIP per NON-EMPTY manifest category, `ZIP_STORED` (deliverables are already
compressed media/text). A pack never nests another `pack_*.zip` or the
`formats_manifest.json` — those are packaging/meta artifacts, not deliverables.
"""
from __future__ import annotations

import zipfile
from pathlib import Path


def _is_packaging_artifact(name: str) -> bool:
    return name == "formats_manifest.json" or (
        name.startswith("pack_") and name.endswith(".zip")
    )


def build_category_packs(out_dir: Path, manifest: dict) -> list[Path]:
    """Write `pack_<category>.zip` for each non-empty category in `manifest`.

    Returns the list of pack paths created. Skips empty categories entirely.
    """
    packs: list[Path] = []
    for category, entries in (manifest.get("categories") or {}).items():
        files: list[Path] = []
        seen: set[str] = set()
        for entry in entries or []:
            name = entry.get("file")
            if not name or name in seen or _is_packaging_artifact(name):
                continue
            path = out_dir / name
            if path.exists():
                files.append(path)
                seen.add(name)
        if not files:
            continue
        pack_path = out_dir / f"pack_{category}.zip"
        with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as zf:
            for path in files:
                zf.write(path, arcname=path.name)
        packs.append(pack_path)
    return packs
