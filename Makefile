.PHONY: help test

help:
	@echo "make test    run the test suite (pytest via uv)"

test:
	uv run --group dev python -m pytest tests/ -q
