# Makefile
.PHONY: up down migrate revision test check fmt
up:        ; docker compose up -d --build
down:      ; docker compose down
migrate:   ; uv run alembic upgrade head
revision:  ; uv run alembic revision --autogenerate -m "$(m)"
test:      ; uv run pytest
fmt:       ; uv run ruff format . && uv run ruff check --fix .
check:     ; uv run ruff format --check . && uv run ruff check . && uv run mypy app