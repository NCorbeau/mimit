.PHONY: bootstrap db-up db-down migrate test test-integration lint format typecheck check
bootstrap:
	uv sync --locked

db-up:
	docker compose up -d --wait

db-down:
	docker compose down

migrate:
	uv run alembic upgrade head

test:
	uv run pytest -m 'not integration'

test-integration:
	uv run pytest -m integration

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

check: lint typecheck test
