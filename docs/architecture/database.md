# Database: Connections & Migrations

Stack: PostgreSQL, SQLAlchemy 2.x (synchronous), psycopg, Alembic, `pydantic-settings`.

This doc covers how the backend connects to PostgreSQL and how schema
migrations are authored and applied, with a focus on what changes between
local development and production. It reflects the actual code in
`backend/app/db/`, `backend/app/config.py`, and `backend/alembic/` — update
it if those change.

## 1. Configuration

There is exactly one source of database configuration: `Settings.DATABASE_URL`
(`backend/app/config.py`), loaded via `pydantic-settings`. Nothing else in
the codebase should read `DATABASE_URL` directly or construct a second
engine.

```
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/<database>
```

- The `+psycopg` driver suffix is required — it selects psycopg 3, the
  dependency this project uses (`psycopg[binary]`), not `psycopg2`.
- Locally, `DATABASE_URL` comes from `backend/.env` (gitignored — see
  `backend/.env.example` for the shape). Never commit `.env`.
- In production, `DATABASE_URL` must come from the deployment platform's
  secret manager / environment injection (see §3), never from a file
  checked into the repo or baked into an image.

## 2. How the connection is wired

`backend/app/db/session.py`:

- `get_engine()` — builds the SQLAlchemy `Engine` lazily, cached with
  `@lru_cache`. Nothing opens a connection at import time; the engine is
  only constructed the first time it's needed. This matters for tests and
  for any tooling that imports the app without a reachable database.
- `get_session_factory()` — a cached `sessionmaker` bound to that engine.
- `get_db()` — the FastAPI dependency. Yields a `Session`, closes it in a
  `finally` block regardless of success or exception.

All request-scoped DB access should go through `Depends(get_db)`. All
Alembic access goes through `alembic/env.py`, which pulls the same
`Settings().DATABASE_URL` — there is no separate hardcoded URL in
`alembic.ini`.

Sessions are synchronous by design (see `backend/app/db/session.py`) — do
not introduce `asyncpg` or async SQLAlchemy sessions without a concrete,
agreed-on reason. This is a deliberate constraint, not an oversight.

## 3. Production connection requirements

### 3.1 Secrets

- `DATABASE_URL` (including the password) must be injected as an
  environment variable by the hosting platform (e.g. Fly.io secrets, Render
  environment groups, AWS Secrets Manager → ECS task definition, Railway
  variables). Treat it exactly like any other credential.
- Never log `DATABASE_URL`, never put it in error messages, never print it
  in CI output. If you need to debug a connection issue in production logs,
  log the host/port/database name only, never the credential portion.
- Rotate the database password through the same secret manager; nothing in
  application code should need to change when the password rotates, only
  the injected environment variable.

### 3.2 TLS

Managed Postgres providers (RDS, Supabase, Neon, Render, Fly Postgres,
Cloud SQL) require or strongly default to SSL. Add `sslmode=require` (or
stricter, e.g. `sslmode=verify-full` with a CA bundle if the provider
supports it) to the production `DATABASE_URL`:

```
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname?sslmode=require
```

Do not disable SSL to work around a connection error — diagnose the actual
certificate/host issue instead.

### 3.3 Connection pooling

`get_engine()` currently calls `create_engine(url, pool_pre_ping=True)` with
SQLAlchemy's default `QueuePool` (`pool_size=5`, `max_overflow=10`).
`pool_pre_ping` guards against stale connections after a database restart
or load-balancer idle timeout — keep this on in production.

Before scaling past a single small instance, revisit pool sizing explicitly:
`pool_size` × (number of app processes/workers) must stay comfortably under
the database's `max_connections`. A managed Postgres tier with a low
connection cap (common on free/hobby tiers) combined with several Uvicorn
workers is a common way to exhaust connections — check the provider's
`max_connections` before choosing worker/pool counts, and consider a
connection pooler (e.g. PgBouncer, or the provider's built-in pooler) if
you need more app processes than the database can directly support.

### 3.4 Networking

- Prefer a private network path (VPC peering, provider-internal networking)
  between the app and the database over exposing Postgres on the public
  internet.
- If the database must be reachable from a laptop for one-off admin work,
  use the provider's bastion/SSH-tunnel mechanism rather than opening the
  database's public ingress broadly.

## 4. Authoring a migration

1. Change the SQLAlchemy models in `backend/app/db/models/`.
2. Generate a migration against a real, disposable Postgres instance
   (autogenerate diffs against the actual running schema, not against
   nothing):
   ```
   cd backend
   uv run alembic revision --autogenerate -m "short description"
   ```
3. **Read the generated migration file.** Autogenerate is a diffing tool,
   not an authority — it can miss things (renamed columns show up as
   drop+add, some type/server-default changes aren't detected, data
   migrations are never generated). Adjust `upgrade()`/`downgrade()` by
   hand where needed.
4. Run it locally end to end:
   ```
   uv run alembic upgrade head
   uv run alembic check      # confirms no drift between models and history
   uv run alembic downgrade -1   # confirm downgrade actually works
   uv run alembic upgrade head
   ```
5. Commit the migration file alongside the model change, in the same PR.
   A model change without its migration (or vice versa) is an incomplete
   change.

Do not use `Base.metadata.create_all()` anywhere outside test fixtures
(`backend/tests/db/conftest.py` uses it deliberately for fast, disposable
SQLite test databases — that is not a migration strategy and must never be
called against a real Postgres database).

## 5. Applying migrations in production

Migrations run as an explicit, separate step from application startup —
**not** inside the FastAPI lifespan hook, and not automatically on app boot.
Running migrations automatically on every app instance's startup is unsafe
with more than one running instance (multiple processes racing to run DDL
concurrently) and makes rollback harder to reason about.

Standard flow for a deploy:

1. Deploy pipeline obtains `DATABASE_URL` for the target environment from
   the secret manager (same variable name the app itself uses).
2. Run, as a one-off task/job (not a long-running process):
   ```
   cd backend
   uv run alembic upgrade head
   ```
3. Only after that step succeeds, roll out the new application version.

Concretely, this is a release-pipeline job/step (e.g. a one-off Fly
machine/Render "pre-deploy command"/GitHub Actions job that runs before the
deploy job/Kubernetes `Job` resource run before the `Deployment` rollout) —
pick whichever your hosting platform calls a pre-deploy hook. The key
invariant is: **schema changes land before the code that depends on them
starts receiving traffic**, and migrations run exactly once, not once per
app replica.

### 5.1 Zero-downtime / backward-compatible changes

Because old and new application code may briefly run against the same
database during a rolling deploy, prefer the expand/contract pattern for
anything more invasive than adding a nullable column:

- **Adding a column**: add it nullable (or with a server default) first;
  backfill; only make it `NOT NULL` in a later migration once all app
  instances write it.
- **Renaming a column**: add the new column, dual-write in application
  code, backfill, switch reads to the new column, then drop the old column
  in a later migration — never a single rename step deployed simultaneously
  with the code that assumes the new name.
- **Dropping a column/table**: only after confirming no running application
  version references it.

This project is small and hackathon-paced, so use judgment about how much
of this ceremony a given change actually needs — but do not casually ship a
migration that would break the currently-running (old) app version mid-
rollout.

### 5.2 Rollback

`alembic downgrade -1` reverses the most recent migration, provided its
`downgrade()` is correct (verified per step 4 above). In practice, for
production incidents the faster and safer response is usually to roll back
the *application* deployment to the previous version rather than downgrade
the schema — only downgrade the schema if the migration itself is the
thing that broke, and you've confirmed the downgrade path is safe against
whatever data now exists (a downgrade written before the migration ran
"against a real database with real rows" can fail in ways it didn't in
testing).

## 6. Verifying a deployment

After migrations run and the new app version is live:

```
uv run alembic current   # should equal the head revision from `alembic heads`
uv run alembic check     # should report no drift
```

If `alembic check` ever reports drift in production, stop and diagnose
before generating a new migration — it means either a migration was
skipped/failed partway, or someone modified the schema outside of Alembic
(e.g. a manual `psql` change). Do not paper over drift by autogenerating a
"fix" migration without understanding how the schema and the migration
history diverged.

## 7. Local development reference

`backend/README.md` is currently empty — this section is the source of
truth for local setup until that changes.

1. Start a local PostgreSQL instance (any install works, as long as it's
   one you control — do not assume port 5432 is free if another Postgres
   install already uses it).
2. Create `backend/.env` from `backend/.env.example`, pointing
   `DATABASE_URL` at that instance and database:
   ```
   DATABASE_URL=postgresql+psycopg://<user>@localhost:<port>/<database>
   ```
3. Run migrations and start the app:
   ```
   cd backend
   uv run alembic upgrade head
   uv run uvicorn app.main:app --reload
   ```
4. Verify: `uv run alembic current` should match `uv run alembic heads`,
   and `curl http://127.0.0.1:8000/api/v1/health` should return
   `{"status":"ok","service":"gapradar-api"}`.
