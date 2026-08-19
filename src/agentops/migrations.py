"""Versioned schema migrations for the SQLite and PostgreSQL backends.

The database module owns the canonical current schema (``SCHEMA``); this module
turns schema evolution into an ordered, idempotent, and locked migration ledger.

Why not Alembic: the project's thesis is a hand-rolled, inspectable database
compatibility layer with no magic. An explicit ordered migration list with
per-dialect guards is on-thesis and easy to audit. See docs/ARCHITECTURE.md.

Concurrency: every migration run is serialized per database. SQLite uses
``BEGIN IMMEDIATE`` (the write lock is acquired before any column check, so two
processes cannot both see a missing column and race the same ALTER TABLE);
PostgreSQL uses ``pg_advisory_lock`` on a fixed key. This matters because
``AGENTOPS_PROCESS_MODE`` can start web, worker, and scheduler processes
against the same database simultaneously, and every process runs
``Database.initialize()`` on startup.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from .database import SCHEMA, Database

# Fixed advisory-lock key shared by every control-plane process ("AGNT").
SCHEMA_LOCK_KEY = 0x4147_4E54

Migration = tuple[str, Callable[[Any, str], None]]


def _statements(schema: str) -> list[str]:
    """Split a DDL script into individual statements.

    sqlite3 executes one statement per ``execute()`` call, and
    ``executescript()`` implicitly commits any open transaction -- which would
    release the migration write lock mid-run. The schema contains no string
    literals, so splitting on ``;`` is safe.
    """
    return [statement.strip() for statement in schema.split(";") if statement.strip()]


def _table_columns(connection: Any, table: str, dialect: str) -> set[str]:
    if dialect == "postgres":
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            (table,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _m_0001_initial(connection: Any, dialect: str) -> None:
    schema = (
        SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        if dialect == "postgres"
        else SCHEMA
    )
    for statement in _statements(schema):
        connection.execute(statement)


def _m_0002_approval_expiry(connection: Any, dialect: str) -> None:
    columns = _table_columns(connection, "approvals", dialect)
    if "expires_at" not in columns:
        connection.execute("ALTER TABLE approvals ADD COLUMN expires_at TEXT")
    if "escalation_level" not in columns:
        connection.execute(
            "ALTER TABLE approvals ADD COLUMN escalation_level INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals(status, expires_at)"
    )


def _m_0003_span_cost_columns(connection: Any, dialect: str) -> None:
    columns = _table_columns(connection, "spans", dialect)
    for name, definition in {
        "input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cost_usd": "REAL NOT NULL DEFAULT 0",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE spans ADD COLUMN {name} {definition}")


def _m_0004_run_controls(connection: Any, dialect: str) -> None:
    columns = _table_columns(connection, "runs", dialect)
    if "max_steps" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN max_steps INTEGER NOT NULL DEFAULT 100")
    if "actor_role" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN actor_role TEXT NOT NULL DEFAULT 'admin'")


def _m_0005_evaluation_progress(connection: Any, dialect: str) -> None:
    columns = _table_columns(connection, "evaluations", dialect)
    for name, definition in {
        "status": "TEXT NOT NULL DEFAULT 'completed'",
        "completed_cases": "INTEGER NOT NULL DEFAULT 0",
        "pass_rate_min": "REAL",
        "max_cost_usd": "REAL",
        "max_p95_latency_ms": "REAL",
        "gate": "TEXT",
        "gate_reasons": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE evaluations ADD COLUMN {name} {definition}")


def _m_0006_outbox(connection: Any, dialect: str) -> None:
    id_type = (
        "BIGSERIAL PRIMARY KEY" if dialect == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS outbox_events ("
        f" id {id_type},"
        " event TEXT NOT NULL,"
        " destination TEXT NOT NULL,"
        " payload TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " attempts INTEGER NOT NULL DEFAULT 0,"
        " next_attempt_at TEXT,"
        " last_error TEXT,"
        " idempotency_key TEXT NOT NULL UNIQUE,"
        " created_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox_events(status, next_attempt_at)"
    )


def _m_0007_outbox_claims(connection: Any, dialect: str) -> None:
    columns = _table_columns(connection, "outbox_events", dialect)
    if "claimed_at" not in columns:
        connection.execute("ALTER TABLE outbox_events ADD COLUMN claimed_at TEXT")


def _m_0008_run_idempotency(connection: Any, dialect: str) -> None:
    columns = _table_columns(connection, "runs", dialect)
    if "idempotency_key" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN idempotency_key TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_idem "
        "ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )


MIGRATIONS: list[Migration] = [
    ("0001_initial", _m_0001_initial),
    ("0002_approval_expiry", _m_0002_approval_expiry),
    ("0003_span_cost_columns", _m_0003_span_cost_columns),
    ("0004_run_controls", _m_0004_run_controls),
    ("0005_evaluation_progress", _m_0005_evaluation_progress),
    ("0006_outbox", _m_0006_outbox),
    ("0007_outbox_claims", _m_0007_outbox_claims),
    ("0008_run_idempotency", _m_0008_run_idempotency),
]


@contextmanager
def _migration_lock(connection: Any, dialect: str) -> Iterator[None]:
    """Serialize schema migration across processes.

    SQLite: ``BEGIN IMMEDIATE`` takes the write lock up front so column checks
    and ALTERs happen under the same lock. PostgreSQL: a session-level advisory
    lock on a fixed key. Both are released when the surrounding
    ``Database.connect()`` context commits or rolls back.
    """
    if dialect == "postgres":
        connection.execute("SELECT pg_advisory_lock(?)", (SCHEMA_LOCK_KEY,))
        try:
            yield
        finally:
            connection.execute("SELECT pg_advisory_unlock(?)", (SCHEMA_LOCK_KEY,))
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_connection(connection: Any, dialect: str) -> list[str]:
    """Apply pending migrations on an open connection and return the applied versions."""
    applied_versions: list[str] = []
    with _migration_lock(connection, dialect):
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
        applied = {row["version"] for row in rows}
        for version, fn in MIGRATIONS:
            if version in applied:
                continue
            fn(connection, dialect)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, _dt.datetime.now(_dt.UTC).isoformat()),
            )
            applied_versions.append(version)
    return applied_versions


def migrate(database: Database) -> list[str]:
    """Apply pending migrations and return the versions applied in this run.

    Idempotent: versions already present in ``schema_migrations`` are skipped,
    and every migration is itself guarded (column existence checks) so a fresh
    database that already contains the current schema records the versions
    without redundant ALTERs.
    """
    dialect = "postgres" if database.is_postgres else "sqlite"
    with database.connect() as connection:
        if dialect == "sqlite":
            # WAL is persistent per file; converting mode on every runtime
            # connection would take a mode-change lock, so set it once here,
            # best-effort. The rare fresh-file race is suppressed: WAL will
            # persist from whichever process converts first.
            row = connection.execute("PRAGMA journal_mode").fetchone()
            if row is None or str(row[0]).lower() != "wal":
                with suppress(Exception):
                    connection.execute("PRAGMA journal_mode = WAL")
        return _migrate_connection(connection, dialect)
