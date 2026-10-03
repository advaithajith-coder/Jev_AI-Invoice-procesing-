# ADR 002: SQLite for local development, PostgreSQL in production

**Status:** Accepted, 2026-10-02

## Context

The first plan ran PostgreSQL and Redis in Docker for every local run. On the development
machine Docker Desktop would not start, and requiring a database server makes the project
harder to try. The pipeline currently processes uploads inline, so Redis is not needed yet.

## Decision

- The schema is portable: `JSON` (becomes `JSONB` on PostgreSQL), generic `Uuid`, CHECK
  constraints without dialect-specific operators, partial indexes declared for both dialects.
- Default `DATABASE_URL` is `sqlite+aiosqlite:///./data/jev_invoice.db`. Foreign keys and WAL
  are switched on per connection.
- Deployed environments use PostgreSQL (`postgresql+asyncpg://...`). Only there does the
  initial migration add triggers that make `decisions` and `audit_events` append-only.
- Alembic runs in batch mode on SQLite so later `ALTER`s work.
- Redis is optional (`REDIS_URL` unset by default) and only needed for the arq worker.
- Tests run each case on its own SQLite file; one test applies the migrations and runs
  `alembic check` to prove models and migrations agree.

## Consequences

- `uv sync && uv run poe setup && uv run poe dev` is enough to run everything.
- SQLite cannot enforce append-only rows; that guarantee holds on PostgreSQL only.
- Moving uploads to a background queue later means turning on Redis and the worker.
