"""Keeps the database schema in step with the code.

The web app calls `ensure_schema()` at start-up:
* AUTO_MIGRATE=true (the default outside production) applies any pending
  migrations, so running `uvicorn` directly after pulling new code just works.
* Otherwise it refuses to start with a clear message instead of failing later
  with "column ... does not exist". (In Docker the entrypoint migrates first.)
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

from .config import get_settings

log = logging.getLogger("teampath.migrate")
ROOT = Path(__file__).resolve().parents[1]


class SchemaOutOfDate(RuntimeError):
    pass


def _config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def status() -> tuple[str | None, str]:
    """(current revision in the database, newest revision in the code)."""
    head = ScriptDirectory.from_config(_config()).get_current_head()
    eng = create_engine(get_settings().migration_database_url, poolclass=pool.NullPool)
    try:
        with eng.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        eng.dispose()
    return current, head


def ensure_schema() -> None:
    s = get_settings()
    try:
        current, head = status()
    except Exception as e:  # can't reach the database with the migration role
        log.warning("Couldn't check the database version (%s). Make sure `alembic upgrade head` has been run.", e)
        return
    if current == head:
        return
    if s.auto_migrate:
        log.warning("Database is at %s; upgrading to %s ...", current or "an empty schema", head)
        command.upgrade(_config(), "head")
        log.warning("Database upgraded to %s.", head)
        return
    raise SchemaOutOfDate(
        f"The database is at revision {current or 'none'} but this code needs {head}. "
        "Run `alembic upgrade head` (or set AUTO_MIGRATE=true) and start the app again."
    )
