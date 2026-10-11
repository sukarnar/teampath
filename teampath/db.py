"""Database engine and tenant-scoped sessions.

Every unit of work runs in one transaction that first sets the row-level
security context (app.org_id, app.user_id, app.role, app.user_path, ...).
Postgres policies read those settings, so a query that forgets to filter by
tenant or hierarchy still cannot return rows the caller may not see.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import UserDefinedType

from .config import get_settings

if TYPE_CHECKING:
    from .context import TenantContext


class Ltree(UserDefinedType):
    """Postgres ltree column (hierarchy paths such as 'n1a2b.n3c4d')."""

    cache_ok = True

    def get_col_spec(self, **kw):
        return "LTREE"

    def bind_processor(self, dialect):
        return lambda v: v

    def result_processor(self, dialect, coltype):
        return lambda v: v


_engine = None
_SessionLocal: sessionmaker | None = None


def engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine():
    """Used by tests after switching DATABASE_URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def _set(db: Session, key: str, value) -> None:
    db.execute(text("select set_config(:k, :v, true)"), {"k": key, "v": "" if value is None else str(value)})


@contextmanager
def tenant_session(ctx: "TenantContext") -> Iterator[Session]:
    """A transaction bound to one user acting inside one organization."""
    engine()
    db = _SessionLocal()
    try:
        with db.begin():
            _set(db, "app.user_id", ctx.user_id)
            _set(db, "app.org_id", ctx.org_id)
            _set(db, "app.role", ctx.role)
            _set(db, "app.user_path", ctx.path)
            _set(db, "app.activity_upline", "on" if ctx.activity_visible_upline else "off")
            _set(db, "app.bypass", "off")
            yield db
    finally:
        db.close()


@contextmanager
def user_session(user_id) -> Iterator[Session]:
    """A transaction for a signed-in user before an organization is chosen.

    Only rows that belong to the user themselves are visible (their own user
    row, memberships and auth tokens).
    """
    engine()
    db = _SessionLocal()
    try:
        with db.begin():
            _set(db, "app.user_id", user_id)
            _set(db, "app.org_id", None)
            _set(db, "app.role", None)
            _set(db, "app.user_path", None)
            _set(db, "app.bypass", "off")
            yield db
    finally:
        db.close()


@contextmanager
def system_session(reason: str) -> Iterator[Session]:
    """Privileged, cross-tenant access. Use only for: authentication lookups,
    platform-admin screens, and background jobs. `reason` is required so every
    call site documents why it needs to bypass tenant isolation."""
    if not reason:
        raise ValueError("system_session requires a reason")
    engine()
    db = _SessionLocal()
    try:
        with db.begin():
            _set(db, "app.bypass", "on")
            _set(db, "app.bypass_reason", reason)
            yield db
    finally:
        db.close()
