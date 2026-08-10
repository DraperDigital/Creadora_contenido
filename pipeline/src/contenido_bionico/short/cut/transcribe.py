"""Thin script shim for the short (vertical) pipeline.

The real transcriber lives in `contenido_bionico.shared.cut.transcribe`.
`pipeline.py` runs this file by path (`sys.executable .../short/cut/transcribe.py
<input> -o <output>`), so it must stay executable as a standalone script.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure `from contenido_bionico...` resolves when this file is invoked as a script.
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from contenido_bionico.shared.cut.transcribe import main  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
