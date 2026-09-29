COMPOSE := docker compose --env-file .env.stack

ROWS ?= 100000000

.PHONY: secrets up down nuke ps logs lint test bench

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
	cd etl && uv run ruff check src tests ../bench && uv run ruff format --check src tests ../bench && uv run mypy

test:
	cd generator && uv run pytest -q
	cd etl && uv run pytest -q

bench: secrets  ## pauses the live stack, benchmarks on a dedicated server, restores
	$(COMPOSE) stop
	$(COMPOSE) --profile bench up -d --wait ch-bench
	set -a && . ./.env.stack && set +a && cd etl && uv run python ../bench/run.py --rows $(ROWS) $(ARGS)
	$(COMPOSE) --profile bench stop ch-bench
	$(COMPOSE) up -d --wait
