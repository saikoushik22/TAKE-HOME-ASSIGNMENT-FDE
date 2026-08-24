# The Lenny Growth Assistant — operator commands.
#
# `make help` lists everything. The three you need on day one:
#   make up        start the whole stack
#   make ingest    populate the knowledge base
#   make test      run the automated suite

SHELL := /bin/sh
COMPOSE := docker compose
PY := backend/.venv/bin/python
ifeq ($(OS),Windows_NT)
	PY := backend/.venv/Scripts/python.exe
endif

.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps build ingest ingest-full ingest-smoke \
        reset-index reset-all test test-unit eval lint dev-backend dev-frontend \
        install health shell-db

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ stack

up: ## Start the full stack (db + backend + frontend)
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  UI       http://localhost:8080"
	@echo "  API      http://localhost:8000/docs"
	@echo "  Health   http://localhost:8000/api/health/ready"
	@echo ""
	@echo "  Corpus not ingested yet? Run: make ingest"

down: ## Stop the stack (keeps the database volume)
	$(COMPOSE) down

restart: ## Restart the backend only
	$(COMPOSE) restart backend

build: ## Rebuild images without starting
	$(COMPOSE) build

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail all logs
	$(COMPOSE) logs -f --tail=100

health: ## Print readiness with per-dependency detail
	@curl -s http://localhost:8000/api/health/ready | $(PY) -m json.tool || \
		echo "Backend not reachable on :8000"

shell-db: ## Open a psql shell
	$(COMPOSE) exec db psql -U lenny -d lenny

# -------------------------------------------------------------- ingestion

ingest: ## Ingest transcripts (incremental; only what changed)
	cd backend && ../$(PY) -u -m scripts.ingest

ingest-smoke: ## Ingest 10 episodes for a fast check
	cd backend && ../$(PY) -u -m scripts.ingest --limit 10

ingest-full: ## Re-ingest and re-embed EVERYTHING (slow on CPU)
	cd backend && ../$(PY) -u -m scripts.ingest --force

stats: ## Print corpus statistics
	cd backend && ../$(PY) -m scripts.ingest --stats

reset-index: ## Drop chunks and embeddings, keep conversations
	$(COMPOSE) exec db psql -U lenny -d lenny \
		-c "TRUNCATE chunks, embedding_cache; DELETE FROM episodes;"
	@echo "Index cleared. Run 'make ingest' to rebuild."

reset-all: ## Destroy the database volume entirely
	$(COMPOSE) down -v
	@echo "Volume removed. 'make up' will start from empty."

# ------------------------------------------------------------------ tests

install: ## Install backend + frontend dev dependencies
	$(PY) -m pip install -r backend/requirements-dev.txt
	cd frontend && npm install

test: ## Run the automated test suite
	cd backend && ../$(PY) -m pytest -q

test-unit: ## Run only tests that need no database
	cd backend && ../$(PY) -m pytest -q -m "not integration"

eval: ## Score the golden set (CBAR + abstention)
	cd backend && ../$(PY) -m scripts.evaluate

# -------------------------------------------------------------- local dev

dev-backend: ## Run the backend natively with reload
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --port 8000

dev-frontend: ## Run the Vite dev server
	cd frontend && npm run dev
