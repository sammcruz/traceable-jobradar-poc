# Needs Python 3.11+. macOS ships 3.9, so we look for a newer one (Homebrew or uv) explicitly.
PYTHON ?= $(shell for p in python3.13 python3.12 python3.11; do command -v $$p >/dev/null 2>&1 && { echo $$p; break; }; done)
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install check-python models lineage-up lineage-down run demo app test lint clean

check-python:
	@if [ -z "$(PYTHON)" ]; then \
	  echo "Python 3.11+ not found (macOS's built-in python3 is 3.9)."; \
	  echo "Install one:  brew install python@3.12   or   brew install uv && uv python install 3.12"; \
	  echo "Or point to it:  make install PYTHON=/path/to/python3.12"; exit 1; fi
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
	  || { echo "$(PYTHON) is older than 3.11"; exit 1; }
	@echo "Using $$($(PYTHON) --version) at $$(command -v $(PYTHON))"

install: check-python  ## Python package with CV (Docling) and web page extras
	rm -rf $(VENV)
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -U pip
	$(BIN)/pip install -e ".[cv,app,dev]"
	@test -f .env || cp .env.example .env
	@echo "Done. Activate with: source $(VENV)/bin/activate"

models:             ## Pull the default local models into Ollama
	ollama pull qwen3:8b
	ollama pull bge-m3

lineage-up:         ## Start Marquez (UI on http://localhost:3000)
	docker compose up -d

lineage-down:
	docker compose down

run:                ## Real data: fetch last week's ads, enrich, deep-enrich tech ads with the LLM
	$(BIN)/jobradar ingest
	$(BIN)/jobradar enrich
	$(BIN)/jobradar enrich --deep -c data -c ai_ml -c software_development -c it_ops_cloud --limit 50
	$(BIN)/jobradar stats

demo:               ## Offline: sample data and fake models, no network needed
	rm -rf data-demo
	JOBRADAR_PROVIDER=fake JOBRADAR_DATA_DIR=./data-demo OPENLINEAGE_URL= $(BIN)/jobradar demo

app:
	$(BIN)/jobradar app

test:
	$(BIN)/pytest -q

lint:
	$(BIN)/ruff check src tests app

clean:              ## Delete all local data (job ads, CV files, vectors, lineage log)
	rm -rf data data-demo
