.PHONY: lint test install

install:
	pip install -e ".[test]"

lint:
	ruff check .

test:
	pytest -q

check: lint test
