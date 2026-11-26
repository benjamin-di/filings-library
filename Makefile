.PHONY: bootstrap check test

bootstrap:
	uv sync --locked --extra extractor

check:
	uv lock --check
	uv run --locked --extra extractor ruff check src tests
	uv run --locked --extra extractor ruff format --check src tests
	uv run --locked --extra extractor mypy src
	ENFORCE_SKIP_LEDGER=1 uv run --locked --extra extractor pytest -m 'not live' -q
	git diff --check

test:
	ENFORCE_SKIP_LEDGER=1 uv run --locked --extra extractor pytest -m 'not live' -q
