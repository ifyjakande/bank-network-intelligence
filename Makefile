COMPOSE := docker compose --env-file .env.stack

.PHONY: secrets up down nuke ps logs lint test

secrets:
	@./scripts/gen-secrets.sh

up: secrets
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

nuke:  ## stop and delete all local data
	$(COMPOSE) down -v

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100 $(s)

lint:
	cd generator && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
	cd etl && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy

test:
	cd generator && uv run pytest -q
	cd etl && uv run pytest -q
