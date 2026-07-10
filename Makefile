COMPOSE ?= docker compose
WORKERS ?= 1

.DEFAULT_GOAL := help

.PHONY: help up down build rebuild restart logs logs-backend logs-worker logs-orchestrator logs-frontend ps scale shell-backend shell-worker shell-db shell-redis test test-frontend clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Build and start the full stack (app + sandbox image + observability)
	$(COMPOSE) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

build: ## Build all images (services + sandbox image)
	$(COMPOSE) build

rebuild: ## Build all images from scratch (no cache)
	$(COMPOSE) build --no-cache

restart: ## Restart the stack
	$(COMPOSE) restart

logs: ## Tail logs from all services
	$(COMPOSE) logs -f

logs-backend: ## Tail API logs
	$(COMPOSE) logs -f backend

logs-worker: ## Tail agent worker logs
	$(COMPOSE) logs -f agent-worker

logs-orchestrator: ## Tail sandbox orchestrator logs
	$(COMPOSE) logs -f sandbox-orchestrator

logs-frontend: ## Tail frontend logs
	$(COMPOSE) logs -f frontend

ps: ## Show service status
	$(COMPOSE) ps

scale: ## Scale agent workers, e.g. make scale WORKERS=8
	$(COMPOSE) up -d --scale agent-worker=$(WORKERS)

shell-backend: ## Open a shell in the backend container
	$(COMPOSE) exec backend sh

shell-worker: ## Open a shell in the agent worker container
	$(COMPOSE) exec agent-worker sh

shell-db: ## Open psql against the app database
	$(COMPOSE) exec postgres-app psql -U app -d harness

shell-redis: ## Open redis-cli
	$(COMPOSE) exec redis redis-cli

test: test-frontend ## Run all checks

test-frontend: ## Type-check and build the frontend
	cd frontend && npx tsc --noEmit && npm run build

clean: ## Stop everything and delete volumes (destroys data)
	$(COMPOSE) down -v
