.PHONY: test lint
test:
	pytest -q
lint:
	python -m compileall -q opatchbot
