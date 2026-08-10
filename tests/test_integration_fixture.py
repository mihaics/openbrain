"""Integration smoke test proving the fixture routes to the test database."""
import pytest


@pytest.mark.integration
def test_fixture_targets_test_database(integration_db):
    with integration_db.get_db_cursor() as cursor:
        cursor.execute("SELECT current_database() AS db, count(*) AS n FROM memory")
        row = cursor.fetchone()
    assert row["db"] == "openbrain_test"
    assert row["n"] == 0, "test database must start empty"
