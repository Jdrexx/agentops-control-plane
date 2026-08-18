import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from src.agentops.database import Database, PostgresConnection
from src.agentops.migrations import MIGRATIONS, _migrate_connection, migrate

# Shape of a database created before the migration ledger existed: the current
# tables minus the columns that were later added by ALTER TABLE.
LEGACY_SCHEMA = """
CREATE TABLE approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  step_index INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(run_id, step_index)
);
CREATE TABLE spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  step_index INTEGER NOT NULL,
  step_name TEXT NOT NULL,
  tool TEXT NOT NULL,
  status TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT,
  error TEXT,
  duration_ms REAL NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, step_index)
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL,
  parent_run_id INTEGER,
  status TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT,
  error TEXT,
  current_step INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
"""

ALL_VERSIONS = [version for version, _ in MIGRATIONS]


def _columns(connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_migrates_to_head_and_is_idempotent(tmp_path: Path):
    database = Database(tmp_path / "fresh.db")
    assert migrate(database) == ALL_VERSIONS
    assert migrate(database) == []
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            == len(MIGRATIONS)
        )
        assert {"expires_at", "escalation_level"} <= _columns(connection, "approvals")
        assert {"input_tokens", "output_tokens", "cost_usd"} <= _columns(connection, "spans")
        assert {"max_steps", "actor_role"} <= _columns(connection, "runs")


def test_legacy_sqlite_database_is_upgraded_in_place(tmp_path: Path):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.commit()
    migrate(Database(path))
    with Database(path).connect() as connection:
        assert {"expires_at", "escalation_level"} <= _columns(connection, "approvals")
        assert {"input_tokens", "output_tokens", "cost_usd"} <= _columns(connection, "spans")
        assert {"max_steps", "actor_role"} <= _columns(connection, "runs")
        assert (
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            == len(MIGRATIONS)
        )
        # Legacy data survives the upgrade.
        connection.execute(
            "INSERT INTO runs(workflow_id,status,input,started_at) "
            "VALUES (1,'completed','{}','now')"
        )
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_legacy_database_missing_tables_gets_them_created(tmp_path: Path):
    path = tmp_path / "partial.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE projects (id INTEGER PRIMARY KEY, "
            "name TEXT, description TEXT, created_at TEXT)"
        )
        connection.commit()
    migrate(Database(path))
    with Database(path).connect() as connection:
        for table in ("workflows", "runs", "spans", "approvals", "datasets", "secrets"):
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            assert row is not None, f"{table} was not created"


def test_initialize_uses_the_migration_ledger(tmp_path: Path):
    database = Database(tmp_path / "app.db")
    database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            == len(MIGRATIONS)
        )


def test_concurrent_migrations_do_not_race(tmp_path: Path):
    path = tmp_path / "shared.db"
    results: list[list[str]] = []
    lock = threading.Lock()

    def run() -> None:
        applied = migrate(Database(path))
        with lock:
            results.append(applied)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results[0]) == ALL_VERSIONS
    assert results[1] == []
    with Database(path).connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            == len(MIGRATIONS)
        )


# --- PostgreSQL dialect path (exercised through the translation layer) ---


class Column:
    def __init__(self, name: str):
        self.name = name


class Cursor:
    def __init__(self, rows, names):
        self.rows = list(rows)
        self.description = [Column(name) for name in names]

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class FakePostgres:
    """Records translated queries; serves configurable ledger/column state."""

    def __init__(
        self,
        applied: list[str] | None = None,
        columns: dict[str, list[str]] | None = None,
    ):
        self.applied = list(applied or [])
        self.columns = dict(columns or {})
        self.queries: list[tuple[str, tuple]] = []
        self.altered: list[str] = []

    def execute(self, query: str, params: tuple = ()):
        self.queries.append((query, params))
        upper = query.lstrip().upper()
        if upper.startswith("SELECT VERSION FROM SCHEMA_MIGRATIONS"):
            return Cursor([(version,) for version in self.applied], ["version"])
        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            return Cursor([(name,) for name in self.columns.get(params[0], [])], ["column_name"])
        if upper.startswith("INSERT INTO SCHEMA_MIGRATIONS"):
            self.applied.append(params[0])
            return Cursor([(len(self.applied),)], ["id"])
        if upper.startswith("ALTER TABLE"):
            self.altered.append(query)
        return Cursor([], [])


def _run_postgres_migrations(fake: FakePostgres) -> list[str]:
    connection = PostgresConnection(fake)
    return _migrate_connection(connection, "postgres")


def test_postgres_initial_migration_uses_bigserial():
    fake = FakePostgres()
    _run_postgres_migrations(fake)
    schema_queries = " ".join(query for query, _ in fake.queries)
    assert "BIGSERIAL PRIMARY KEY" in schema_queries
    assert "AUTOINCREMENT" not in schema_queries


def test_postgres_migration_adds_missing_columns_under_advisory_lock():
    fake = FakePostgres(
        columns={
            "approvals": ["id", "run_id", "step_index", "prompt", "status"],
            "spans": ["id", "run_id", "step_index", "step_name", "tool", "status"],
            "runs": ["id", "workflow_id", "parent_run_id", "status", "input"],
        }
    )
    _run_postgres_migrations(fake)
    assert any("pg_advisory_lock" in query for query, _ in fake.queries)
    assert any("pg_advisory_unlock" in query for query, _ in fake.queries)
    add_expiry = "ALTER TABLE approvals ADD COLUMN expires_at TEXT"
    add_escalation = "ALTER TABLE approvals ADD COLUMN escalation_level INTEGER NOT NULL DEFAULT 0"
    assert add_expiry in fake.altered
    assert add_escalation in fake.altered
    assert "ALTER TABLE spans ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0" in fake.altered
    assert "ALTER TABLE runs ADD COLUMN max_steps INTEGER NOT NULL DEFAULT 100" in fake.altered
    assert "ALTER TABLE runs ADD COLUMN actor_role TEXT NOT NULL DEFAULT 'admin'" in fake.altered
    assert all(version in fake.applied for version in ALL_VERSIONS)


def test_postgres_migration_skips_columns_that_already_exist():
    fake = FakePostgres(
        applied=ALL_VERSIONS,
        columns={
            "approvals": [
                "id", "run_id", "step_index", "prompt", "status",
                "expires_at", "escalation_level",
            ],
            "spans": [
                "id", "run_id", "step_index", "step_name", "tool", "status",
                "input_tokens", "output_tokens", "cost_usd",
            ],
            "runs": [
                "id", "workflow_id", "parent_run_id", "status", "input",
                "max_steps", "actor_role",
            ],
        },
    )
    _run_postgres_migrations(fake)
    assert fake.altered == []
    assert "SELECT pg_advisory_lock" in " ".join(query for query, _ in fake.queries)
