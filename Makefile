.PHONY: install test lint typecheck run run-live clean

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src tests

typecheck:
	mypy src

run:
	LIVE_TRADING=false python -m src.bot

run-live:
	@if [ "$$(grep -s LIVE_TRADING .env | cut -d= -f2)" != "true" ]; then \
		echo "ERROR: set LIVE_TRADING=true in .env to enable live mode"; exit 1; \
	fi
	python -m src.bot

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -name "*.pyc" -delete; \
	rm -rf .coverage htmlcov .mypy_cache .ruff_cache dist *.egg-info
