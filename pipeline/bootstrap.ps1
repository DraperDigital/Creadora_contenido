# Bootstrap script for contenido-bionico on a clean Windows machine.
#
# Mirrors bootstrap.sh: installs the only thing the setup wizard cannot
# install for itself -- a Python 3.12 toolchain (via uv). After that, it
# creates a virtualenv, installs the package, and hands off to
# `contenido-bionico setup`, which installs the rest (ffmpeg, Node, Claude
# CLI, Remotion preflight, etc.).
#
# Safe to re-run. Idempotent. Installs everything under your user
# directory; no administrator rights required.
#
# Usage from the repo root (PowerShell):
#   .\bootstrap.ps1
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repoRoot

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Write-Host "==> contenido-bionico bootstrap"
Write-Host "    repo: $repoRoot"
Write-Host ""

# --- 1. Install uv if missing (same toolchain manager as bootstrap.sh). ---
$localBin = Join-Path $env:USERPROFILE ".local\bin"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path -LiteralPath (Join-Path $localBin "uv.exe"))) {
        Write-Host "==> Installing uv (Python toolchain manager)..."
        powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    }
    $env:Path = "$localBin;$env:Path"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv installation failed. Install manually from https://astral.sh/uv and re-run this script."
}
Write-Host "OK: uv $((uv --version) -replace '^uv\s+', '')"
Write-Host ""

# --- 2. Create a Python 3.12 venv in the repo. ---
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "==> Creating Python 3.12 venv at .venv\"
    Invoke-Native -FilePath "uv" -Arguments @("venv", "--python", "3.12")
}
Write-Host "OK: $(& $venvPython --version)"
Write-Host ""

# --- 3. Install the package into the venv. ---
Write-Host "==> Installing contenido-bionico into .venv\"
Invoke-Native -FilePath "uv" -Arguments @("pip", "install", "-e", ".", "--quiet")
Write-Host "OK: contenido-bionico installed"
Write-Host ""

# --- 4. Run setup and verify readiness. ---
$bionico = Join-Path $repoRoot ".venv\Scripts\contenido-bionico.exe"

Write-Host "==> Running setup wizard"
Write-Host ""
Invoke-Native -FilePath $bionico -Arguments @("setup")
Write-Host ""

Write-Host "==> Running contenido-bionico doctor"
Invoke-Native -FilePath $bionico -Arguments @("doctor")
Write-Host ""
Write-Host "OK: contenido-bionico installation is ready."
