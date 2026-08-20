#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
FRONTEND_DIR="${ROOT_DIR}/frontend"
BACKEND_ENV_FILE="${BACKEND_DIR}/.env"
BACKEND_ENV_EXAMPLE="${BACKEND_DIR}/.env.example"
FRONTEND_ENV_FILE="${FRONTEND_DIR}/.env.local"

PG_CONTAINER_NAME="gapradar-postgres"
PG_USER="gapradar"
PG_PASSWORD="gapradar"
PG_DB="gapradar"
PG_PORT="5432"
LOCAL_DATABASE_URL="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"
LOCAL_API_BASE_URL="http://localhost:8000"

log() {
  printf "\n[setup] %s\n" "$1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[setup] Missing required command: $1"
    exit 1
  fi
}

set_or_append_env_var() {
  local file="$1"
  local key="$2"
  local value="$3"

  if grep -Eq "^${key}=" "$file"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf "%s=%s\n" "$key" "$value" >> "$file"
  fi
  rm -f "${file}.bak"
}

ensure_backend_env() {
  if [[ ! -f "$BACKEND_ENV_FILE" ]]; then
    log "Creating backend .env from .env.example"
    cp "$BACKEND_ENV_EXAMPLE" "$BACKEND_ENV_FILE"
  fi

  if grep -Eq '^DATABASE_URL=$' "$BACKEND_ENV_FILE" || ! grep -Eq '^DATABASE_URL=' "$BACKEND_ENV_FILE"; then
    log "Setting backend DATABASE_URL for local postgres"
    set_or_append_env_var "$BACKEND_ENV_FILE" "DATABASE_URL" "$LOCAL_DATABASE_URL"
  fi

  if grep -Eq '^CORS_ORIGINS=$' "$BACKEND_ENV_FILE" || ! grep -Eq '^CORS_ORIGINS=' "$BACKEND_ENV_FILE"; then
    log "Setting backend CORS_ORIGINS"
    set_or_append_env_var "$BACKEND_ENV_FILE" "CORS_ORIGINS" "http://localhost:5173"
  fi

  if ! grep -Eq '^GAPRADAR_MCP_API_KEY=' "$BACKEND_ENV_FILE" || grep -Eq '^GAPRADAR_MCP_API_KEY=$' "$BACKEND_ENV_FILE"; then
    log "Generating GAPRADAR_MCP_API_KEY for local MCP route"
    local mcp_key
    mcp_key="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
    set_or_append_env_var "$BACKEND_ENV_FILE" "GAPRADAR_MCP_API_KEY" "$mcp_key"
  fi
}

ensure_frontend_env() {
  if [[ ! -f "$FRONTEND_ENV_FILE" ]]; then
    log "Creating frontend .env.local"
    printf "VITE_API_BASE_URL=%s\n" "$LOCAL_API_BASE_URL" > "$FRONTEND_ENV_FILE"
  elif ! grep -Eq '^VITE_API_BASE_URL=' "$FRONTEND_ENV_FILE"; then
    log "Adding VITE_API_BASE_URL to frontend .env.local"
    printf "VITE_API_BASE_URL=%s\n" "$LOCAL_API_BASE_URL" >> "$FRONTEND_ENV_FILE"
  fi
}

ensure_postgres() {
  if ! command -v docker >/dev/null 2>&1; then
    log "Docker not found; skipping Postgres container setup"
    echo "[setup] Ensure DATABASE_URL in backend/.env points to a reachable Postgres instance."
    return
  fi

  if docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER_NAME"; then
    log "Postgres container already running (${PG_CONTAINER_NAME})"
    return
  fi

  if docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER_NAME"; then
    log "Starting existing Postgres container (${PG_CONTAINER_NAME})"
    docker start "$PG_CONTAINER_NAME" >/dev/null
    return
  fi

  log "Creating and starting Postgres container (${PG_CONTAINER_NAME})"
  docker run -d \
    --name "$PG_CONTAINER_NAME" \
    -e POSTGRES_USER="$PG_USER" \
    -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -e POSTGRES_DB="$PG_DB" \
    -p "${PG_PORT}:5432" \
    postgres:16 >/dev/null
}

install_backend() {
  log "Installing backend dependencies (uv sync --dev)"
  (cd "$BACKEND_DIR" && uv sync --dev --frozen)
}

install_frontend() {
  log "Installing frontend dependencies (npm install)"
  (cd "$FRONTEND_DIR" && npm install)
}

run_migrations() {
  log "Running Alembic migrations"
  (
    cd "$BACKEND_DIR"
    set -a
    # shellcheck disable=SC1090
    source "$BACKEND_ENV_FILE"
    set +a
    uv run alembic upgrade head
  )
}

print_summary() {
  cat <<EOF

Setup complete.

Next commands:
  1) Start backend:  cd backend && uv run uvicorn app.main:app --reload
  2) Start frontend: cd frontend && npm run dev
  3) MCP endpoint:   http://localhost:8000/mcp

Local defaults used:
  - DATABASE_URL: ${LOCAL_DATABASE_URL}
  - Frontend API: ${LOCAL_API_BASE_URL}
  - Postgres container: ${PG_CONTAINER_NAME}
EOF
}

main() {
  require_cmd python3
  require_cmd uv
  require_cmd npm

  ensure_backend_env
  ensure_frontend_env
  ensure_postgres
  install_backend
  install_frontend
  run_migrations
  print_summary
}

main "$@"
