# scripts/check.ps1 — local CI parity (PowerShell, runs on Windows)
#
# Mirrors the exact gates in .github/workflows/ci.yml, in order.
# Exits non-zero on the FIRST failure so you see one clear error at a time.
#
# Usage:
#   pwsh scripts/check.ps1
#   # or with Windows PowerShell 5:
#   powershell -File scripts/check.ps1

$ErrorActionPreference = 'Stop'

Write-Host "==> [1/3] ruff lint"
ruff check apex/ tests/ scripts/
if (-not $?) { exit 1 }

Write-Host "==> [2/3] ruff format --check"
ruff format --check apex/ tests/ scripts/
if (-not $?) { exit 1 }

Write-Host "==> [3/3] pytest"
pytest
if (-not $?) { exit 1 }

Write-Host ""
Write-Host "All gates passed."
