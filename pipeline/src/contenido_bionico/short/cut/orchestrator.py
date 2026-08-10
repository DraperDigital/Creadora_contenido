"""Thin shim for the short (vertical) pipeline.

The real cut orchestrator lives in `contenido_bionico.shared.cut.orchestrator`;
the long and short pipelines run the exact same cut flow. `pipeline.py`
imports this module and calls `main([...])`.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure `from contenido_bionico...` resolves when this file is invoked as a script.
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from contenido_bionico.shared.cut.orchestrator import main  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(main())
