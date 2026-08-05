from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  definition TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, name, version)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  parent_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  status TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT,
  error TEXT,
  current_step INTEGER NOT NULL DEFAULT 0,
  max_steps INTEGER NOT NULL DEFAULT 100,
  actor_role TEXT NOT NULL DEFAULT 'admin',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  step_index INTEGER NOT NULL,
  step_name TEXT NOT NULL,
  tool TEXT NOT NULL,
  status TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT,
  error TEXT,
  duration_ms REAL NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, step_index)
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  step_index INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  decided_at TEXT,
  expires_at TEXT,
  escalation_level INTEGER NOT NULL DEFAULT 0,
  UNIQUE(run_id, step_index)
);
CREATE TABLE IF NOT EXISTS datasets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  cases TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
  passed INTEGER NOT NULL,
  total INTEGER NOT NULL,
  results TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  input TEXT NOT NULL,
  interval_seconds INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at TEXT NOT NULL,
  last_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  events TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  event TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_name TEXT NOT NULL,
  role TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, user_name)
);
CREATE TABLE IF NOT EXISTS secrets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  ciphertext TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  resource TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  metric TEXT NOT NULL,
  threshold REAL NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  namespace TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, namespace, key)
);
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON runs(workflow_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_spans_run ON spans(run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_webhooks_project ON webhooks(project_id, enabled);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_name, project_id);
"""


class CompatRow(Mapping[str, Any]):
    def __init__(self, names: list[str], values: tuple[Any, ...]):
        self.names = names
        self.values = values

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self.values[key]
        return self.values[self.names.index(key)]

    def __iter__(self):
        return iter(self.names)

    def __len__(self) -> int:
        return len(self.names)


class PostgresCursor:
    def __init__(self, cursor: psycopg.Cursor, lastrowid: int | None = None):
        self.cursor = cursor
        self.lastrowid = lastrowid

    def _row(self, value: tuple[Any, ...] | None) -> CompatRow | None:
        if value is None:
            return None
        names = [column.name for column in self.cursor.description or []]
        return CompatRow(names, value)

    def fetchone(self) -> CompatRow | None:
        return self._row(self.cursor.fetchone())

    def fetchall(self) -> list[CompatRow]:
        return [self._row(row) for row in self.cursor.fetchall()]  # type: ignore[misc]

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount


class PostgresConnection:
    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    def execute(self, query: str, params: tuple[Any, ...] | list[Any] = ()) -> PostgresCursor:
        translated = query.replace("?", "%s")
        returns_id = translated.lstrip().upper().startswith("INSERT INTO")
        if returns_id and "RETURNING" not in translated.upper():
            translated = f"{translated.rstrip()} RETURNING id"
        cursor = self.connection.execute(translated, params)
        lastrowid = cursor.fetchone()[0] if returns_id else None
        return PostgresCursor(cursor, lastrowid)

    def executescript(self, script: str) -> None:
        self.connection.execute(script)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.is_postgres = self.path.startswith(("postgresql://", "postgres://"))

    def initialize(self) -> None:
        if not self.is_postgres:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            schema = (
                SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                if self.is_postgres
                else SCHEMA
            )
            connection.executescript(schema)
            if self.is_postgres:
                return
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(approvals)")}
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE approvals ADD COLUMN expires_at TEXT")
            if "escalation_level" not in columns:
                connection.execute(
                    "ALTER TABLE approvals ADD COLUMN escalation_level INTEGER NOT NULL DEFAULT 0"
                )
            span_columns = {row["name"] for row in connection.execute("PRAGMA table_info(spans)")}
            for name, definition in {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "cost_usd": "REAL NOT NULL DEFAULT 0",
            }.items():
                if name not in span_columns:
                    connection.execute(f"ALTER TABLE spans ADD COLUMN {name} {definition}")
            run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
            if "max_steps" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN max_steps INTEGER NOT NULL DEFAULT 100"
                )
            if "actor_role" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN actor_role TEXT NOT NULL DEFAULT 'admin'"
                )

    def ready(self) -> bool:
        """Return whether the configured database accepts a simple query."""
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT 1 AS ready").fetchone()
                return row is not None and row["ready"] == 1
        except Exception:
            return False

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection | PostgresConnection]:
        if self.is_postgres:
            connection = PostgresConnection(psycopg.connect(self.path, connect_timeout=10))
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            return
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
