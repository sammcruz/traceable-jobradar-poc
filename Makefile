.PHONY: install models lineage-up lineage-down run demo app test lint clean

install:            ## Python package with CV (Docling) and web page extras
	python3 -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -e ".[cv,app,dev]"
	@test -f .env || cp .env.example .env

models:             ## Pull the default local models into Ollama
	ollama pull qwen3:8b
	ollama pull bge-m3

lineage-up:         ## Start Marquez (UI on http://localhost:3000)
	docker compose up -d

lineage-down:
	docker compose down

run:                ## Real data: fetch last week's ads, enrich, deep-enrich tech ads with the LLM
	jobradar ingest
	jobradar enrich
	jobradar enrich --deep -c data -c ai_ml -c software_development -c it_ops_cloud --limit 200
	jobradar stats

demo:               ## Offline: sample data and fake models, no network needed
	JOBRADAR_PROVIDER=fake JOBRADAR_DATA_DIR=./data-demo jobradar demo

app:
	jobradar app

test:
	pytest -q

lint:
	ruff check src tests app

clean:              ## Delete all local data (job ads, CV files, vectors, lineage log)
	rm -rf data data-demo
