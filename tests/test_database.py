import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.agentops.database import Database, PostgresConnection
from src.agentops.service import AgentOpsService


def test_database_enforces_foreign_keys(tmp_path: Path):
    database = Database(tmp_path / "foreign.db")
    database.initialize()
    with database.connect() as connection:
        try:
            connection.execute(
                """INSERT INTO workflows(project_id, name, version, definition, created_at)
                   VALUES (1, 'x', 1, '[]', 'now')"""
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("foreign key was not enforced")


def test_transaction_rolls_back_on_error(tmp_path: Path):
    database = Database(tmp_path / "rollback.db")
    database.initialize()
    try:
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO projects(name,description,created_at) VALUES('rolled back','','now')"
            )
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_concurrent_project_writes_are_serialized(tmp_path: Path):
    database = Database(tmp_path / "concurrent.db")
    database.initialize()
    service = AgentOpsService(database)

    def create(index: int):
        return service.create_project(f"Project {index}", "parallel write")

    with ThreadPoolExecutor(max_workers=8) as executor:
        projects = list(executor.map(create, range(40)))
    assert len(projects) == 40
    assert len({project["id"] for project in projects}) == 40
    assert len(service.list_projects()) == 40


def test_postgres_compatibility_adapter_translates_queries():
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

    class Connection:
        def __init__(self):
            self.queries = []

        def execute(self, query, params=()):
            self.queries.append((query, params))
            if query.lstrip().upper().startswith("INSERT"):
                return Cursor([(7,)], ["id"])
            return Cursor([(7, "project")], ["id", "name"])

    raw = Connection()
    connection = PostgresConnection(raw)
    inserted = connection.execute("INSERT INTO projects(name) VALUES(?)", ("project",))
    assert inserted.lastrowid == 7
    assert "%s" in raw.queries[0][0]
    row = connection.execute("SELECT id,name FROM projects WHERE id=?", (7,)).fetchone()
    assert row[0] == 7
    assert row["name"] == "project"
    assert dict(row) == {"id": 7, "name": "project"}
