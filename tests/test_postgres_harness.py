import os

import pytest
from sqlalchemy import create_engine, text


pytestmark = pytest.mark.postgres


def test_postgres_test_database_is_reachable():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT current_database()"))
            assert result.scalar_one() == "envaperu_test"
    finally:
        engine.dispose()
