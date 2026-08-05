.PHONY: lint test test-unit test-care install check

install:
	pip install -e ".[test]"

lint:
	ruff check .

test-unit:
	pytest -q -m "not integration"

test-care:
	pytest -q -m integration

test: test-unit test-care

check: lint test
