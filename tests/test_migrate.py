"""Starting the app against an older database: upgrade automatically, or refuse with a clear message."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
ADMIN = os.environ.get("TEST_PG_ADMIN_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/postgres")


@pytest.fixture
def old_db():
    name = "teampath_migrate_test"
    eng = create_engine(ADMIN, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f"drop database if exists {name} with (force)"))
        c.execute(text(f"create database {name}"))
    url = ADMIN.rsplit("/", 1)[0] + f"/{name}"
    env = {**os.environ, "MIGRATION_DATABASE_URL": url}
    # The database as it was before the feedback and prospects changes.
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0001"], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    yield url
    with eng.connect() as c:
        c.execute(text(f"drop database if exists {name} with (force)"))
    eng.dispose()


def _run(url: str, auto: str) -> subprocess.CompletedProcess:
    code = ("from teampath.migrate import ensure_schema, status; ensure_schema(); print('AT', status())")
    env = {**os.environ, "MIGRATION_DATABASE_URL": url, "AUTO_MIGRATE": auto}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)


def test_refuses_to_start_with_clear_message(old_db):
    r = _run(old_db, "false")
    assert r.returncode != 0
    assert "alembic upgrade head" in r.stderr and "needs" in r.stderr


def test_upgrades_automatically(old_db):
    r = _run(old_db, "true")
    assert r.returncode == 0, r.stderr
    current, head = eval(r.stdout.strip().split("AT ")[1])
    assert current == head
    eng = create_engine(old_db)
    with eng.connect() as c:
        cols = c.execute(text("select column_name from information_schema.columns where table_name='feedback'")).scalars().all()
        assert "seen_at" in cols
        assert c.execute(text("select rowsecurity from pg_tables where tablename='prospects'")).scalar() is True
    eng.dispose()
