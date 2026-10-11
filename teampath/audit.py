"""Append-only audit trail."""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.orm import Session

from .context import TenantContext
from .models import AuditLog


def record(db: Session, ctx: TenantContext | None, action: str, target_type: str | None = None,
           target_id=None, org_id=None, actor_id=None, ip: str | None = None, **detail) -> None:
    # Core insert without RETURNING: most roles may write audit rows but not read them back.
    db.execute(
        insert(AuditLog.__table__).values(
            org_id=org_id or (ctx.org_id if ctx else None),
            actor_id=actor_id or (ctx.user_id if ctx else None),
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            detail={k: (str(v) if not isinstance(v, (int, float, bool, type(None), list, dict)) else v) for k, v in detail.items()},
            ip=ip or (ctx.ip if ctx else None),
        )
    )
