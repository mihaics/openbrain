"""Shared test configuration.

src/main.py is written to run as a script: it inserts the repo root on
sys.path and then does `from db import connection`, which only resolves when
src/ is itself on the path. Putting src/ on the path here makes
`import src.main` work in tests without every test file repeating it.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, REPO_ROOT)


PRODUCTION_DB = "openbrain"
TEST_DB = "openbrain_test"


def _connection_identities():
    """Every loaded copy of the connection module.

    src/main.py and src/api/main.py import `db.connection`; the CLI and tests
    import `src.db.connection`. Python treats those as distinct modules, each
    with its own DatabaseConfig._instance and its own pool. Resetting one and
    asserting on its connection proves nothing about a committed mutation
    routed through the other, so the fixture must reset all of them.
    """
    return [
        mod for name, mod in list(sys.modules.items())
        if name in ("db.connection", "src.db.connection") and mod is not None
    ]


def _reset_connection_state():
    for mod in _connection_identities():
        try:
            mod.get_pool().close_all()
        except Exception:
            pass
        config_cls = getattr(mod, "DatabaseConfig", None)
        if config_cls is not None:
            config_cls._instance = None


@pytest.fixture
def integration_db(monkeypatch):
    """Point every connection identity at openbrain_test, and prove it.

    Fails rather than skips when the database is missing: these tests exist to
    validate SQL that mocked cursors cannot, so a silent skip would quietly
    remove the only coverage of that.
    """
    import importlib

    monkeypatch.setenv("DB_NAME", TEST_DB)
    _reset_connection_state()

    connection = importlib.import_module("src.db.connection")

    try:
        with connection.get_db_cursor() as cursor:
            cursor.execute("SELECT current_database() AS db")
            row = cursor.fetchone()
    except Exception as e:
        pytest.fail(
            f"integration tests require the {TEST_DB} database: {e}\n"
            f"provision it with: sudo -u postgres createdb -p 5433 "
            f"-O openbrain {TEST_DB}"
        )

    actual = row["db"] if isinstance(row, dict) else row[0]
    assert actual != PRODUCTION_DB, (
        f"integration test connected to the PRODUCTION database ({actual})"
    )
    assert actual == TEST_DB, f"expected {TEST_DB}, connected to {actual}"

    yield connection

    _reset_connection_state()
