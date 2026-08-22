.PHONY: setup backend frontend test lint

setup:
	bash scripts/setup_local.sh

backend:
	cd backend && uv run uvicorn app.main:app --reload

frontend:
	cd frontend && npm run dev

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check .
