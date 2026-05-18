# Running the Real-PostgreSQL Integration Test Suite

## Overview

Most ACN tests use in-memory fakes or mock repositories and run entirely
without a real database.  A small subset of tests — marked
`pytest.mark.integration` — require a live PostgreSQL instance because
they validate behaviour that only a real engine can exercise:

| Test file | What it guards |
|-----------|---------------|
| `tests/integration/test_settlement_outbox_pg.py` | `SKIP LOCKED` prevents double-claim; same-tx atomicity of outbox enqueue |
| `tests/integration/test_pg_subnet_cascade.py` | `delete_with_children` atomically commits or rolls back under real Postgres (ADR-0003 §A.4) |

## Prerequisites

* Docker (or a locally running PostgreSQL ≥ 14)
* Python environment with `asyncpg` (already in `pyproject.toml`)

## Quickstart — Docker one-liner

```bash
# Start a throwaway Postgres container
docker run --rm -d \
  --name acn-test-pg \
  -e POSTGRES_USER=acn \
  -e POSTGRES_PASSWORD=acn \
  -e POSTGRES_DB=acn_test \
  -p 5432:5432 \
  postgres:16-alpine

# Wait for PG to be ready (usually 2–3 s)
sleep 3

# Point the integration suite at it
export ACN_INTEGRATION_PG_URL="postgresql+asyncpg://acn:acn@localhost:5432/acn_test"

# Run only integration tests (the rest of the suite is unaffected)
cd acn
uv run pytest -m integration -v

# Tear down
docker stop acn-test-pg
```

## Running all tests (unit + integration) together

```bash
export ACN_INTEGRATION_PG_URL="postgresql+asyncpg://acn:acn@localhost:5432/acn_test"
cd acn
uv run pytest -v
```

Without `ACN_INTEGRATION_PG_URL` the integration tests emit a `SKIP`
and the default `pytest` run stays fast.

## CI strategy

The standard CI job (`python` in `.github/workflows/ci.yml`) does NOT
set `ACN_INTEGRATION_PG_URL`, so integration tests are skipped on every
PR.

A separate opt-in **Integration** job in `.github/workflows/ci.yml` runs
the full real-PG suite.  It is triggered by:

* A PR label `run-integration-tests` being added, **or**
* The nightly scheduled run (daily at 02:00 UTC on `main`).

To trigger integration tests on your PR:

```
gh pr edit <pr-number> --add-label run-integration-tests
```

## What each integration test does

### `test_pg_subnet_cascade.py`

| Test | Scenario |
|------|----------|
| `test_delete_with_children_happy_path` | Inserts parent + 3 children; calls `delete_with_children`; asserts all 4 rows are gone and the return value is `True`. |
| `test_delete_with_children_rollback_on_mid_cascade_failure` | Injects a `RuntimeError` on the **second** `session.execute` call inside `session.begin()`; asserts the exception propagates AND all rows still exist (full ROLLBACK). |
| `test_delete_with_children_parent_absent` | Calls `delete_with_children` when neither parent nor children exist; asserts `False` is returned without raising. |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `asyncpg.InvalidCatalogNameError` | `acn_test` DB not created | `createdb acn_test` or pass a different DB name |
| `asyncpg.InvalidPasswordError` | Wrong credentials in DSN | Update `ACN_INTEGRATION_PG_URL` |
| `connection refused` | PG not running or wrong port | Check `docker ps` / port mapping |
| Index-already-exists DDL error | Stale schema from a previous aborted run | The fixture DROPs indexes before the table; if the DROP is skipped, manually run `DROP INDEX IF EXISTS subnets_parent_idx, subnets_linked_task_idx;` |
