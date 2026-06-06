#!/usr/bin/env sh
# scripts/check.sh — local CI parity (POSIX sh, runs on Linux/macOS/WSL)
#
# Mirrors the exact gates in .github/workflows/ci.yml, in order.
# Exits non-zero on the FIRST failure so you see one clear error at a time.
#
# Usage:
#   bash scripts/check.sh

set -e

echo "==> [1/3] ruff lint"
ruff check apex/ tests/ scripts/

echo "==> [2/3] ruff format --check"
ruff format --check apex/ tests/ scripts/

echo "==> [3/3] pytest"
pytest

echo ""
echo "All gates passed."
