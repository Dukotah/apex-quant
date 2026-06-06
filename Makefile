# Apex Quant — developer convenience targets
# Mirrors the exact gates that CI runs (.github/workflows/ci.yml).
#
# Usage:
#   make check   — lint + format-check + test (fails on first error, same as CI)
#   make fmt     — auto-format all source with ruff
#   make test    — run the full pytest suite

.PHONY: check fmt test

# Run the exact three CI gates in order, failing fast.
check:
	ruff check apex/ tests/ scripts/
	ruff format --check apex/ tests/ scripts/
	pytest

# Auto-format source (not --check, just fix).
fmt:
	ruff format apex/ tests/ scripts/

# Full test suite (coverage config lives in pyproject.toml addopts).
test:
	pytest
