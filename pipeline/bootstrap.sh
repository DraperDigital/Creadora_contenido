#!/usr/bin/env bash
# Bootstrap script for contenido-bionico on a clean machine.
#
# Installs the only thing the setup wizard cannot install for itself: a
# Python 3.11+ toolchain (via uv). After that, it creates a virtualenv,
# installs the package, and hands off to `contenido-bionico setup`, which
# installs the rest (ffmpeg, Node, Claude CLI, Remotion preflight,
# macOS permission preflight, etc.).
#
# Safe to re-run. Idempotent. Installs everything under your user
# directory; no sudo required.
#
# Usage from the repo root:
#   ./bootstrap.sh

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

echo "==> contenido-bionico bootstrap"
echo "    repo: $repo_root"
echo ""

# --- 1. Install uv if missing. ---
if ! command -v uv >/dev/null 2>&1; then
  if [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "==> Installing uv (Python toolchain manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
fi
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv installation failed. Install manually from https://astral.sh/uv and re-run."
  exit 1
fi
echo "OK: uv $(uv --version | awk '{print $2}')"
echo ""

# --- 2. Create a Python 3.12 venv in the repo. ---
if [ ! -d ".venv" ] || [ ! -x ".venv/bin/python3" ]; then
  echo "==> Creating Python 3.12 venv at .venv/"
  uv venv --python 3.12
fi
echo "OK: $(.venv/bin/python3 --version)"
echo ""

# --- 3. Install the package into the venv. ---
echo "==> Installing contenido-bionico into .venv/"
uv pip install -e . --quiet
echo "OK: contenido-bionico installed"
echo ""

# --- 4. Run setup and verify readiness. ---
echo "==> Running setup wizard"
echo ""
.venv/bin/contenido-bionico setup
echo ""
echo "==> Running contenido-bionico doctor"
.venv/bin/contenido-bionico doctor
echo ""
echo "OK: contenido-bionico installation is ready."
