"""Background tasks: virus scanning, document previews, logo processing,
momentum refresh, question escalation and housekeeping."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import delete, select

from .. import audit, events, previews, storage
from ..config import get_settings
from ..db import system_session
from ..models import (
    AuthToken,
    ContentVersion,
    Membership,
    Organization,
    OrgBranding,
    StoredFile,
    Thread,
    User,
)
from ..services.activity import refresh_momentum
from ..services.notify import notify
from ..services.orgs import upline_chain

log = logging.getLogger("teampath.worker")


class Retry(Exception):
    """Re-queue the job after a delay."""

    def __init__(self, seconds: int = 10):
        self.seconds = seconds


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------ antivirus

def scan_file(file_id: str, **_) -> None:
    """Only used when the scanner was unreachable at upload time."""
    with system_session("worker: scan uploaded file") as db:
        f = db.get(StoredFile, uuid.UUID(file_id))
        if not f or f.scan_status != "pending":
            return
        path = storage.absolute(f.storage_path)
        try:
            status = previews.clamd_scan(path) if get_settings().clamd_host else "skipped"
        except previews.ScannerUnavailable:
            raise Retry(10)  # ClamAV can take a few minutes to load signatures after a restart
        f.scan_status = status
        if status == "infected":
            path.unlink(missing_ok=True)
            audit.record(db, None, "file.infected", "file", f.id, org_id=f.org_id, actor_id=f.owner_id)
            log.warning("infected upload removed: %s", f.id)
        org_id = f.org_id
    events.publish(events.org_topic(org_id), kind="content", file_id=file_id)


# ------------------------------------------------------------ previews

def convert_document(version_id: str, **_) -> None:
    with system_session("worker: load content version for preview") as db:
        v = db.get(ContentVersion, uuid.UUID(version_id))
        f = db.get(StoredFile, v.file_id) if v and v.file_id else None
        if not v or not f or v.preview_status == "ready":
            return
        if f.scan_status == "pending":
            raise Retry(5)
        if f.scan_status not in ("clean", "skipped"):
            v.preview_status = "failed"
            return
        rel = f.storage_path
    try:
        if Path(rel).suffix.lower() == ".pdf":
            pages = previews.pdf_page_count(storage.absolute(rel))
        else:
            pages = previews.convert_office_to_pdf(rel)
        status = "ready"
    except Exception:
        log.exception("preview failed for version %s", version_id)
        pages, status = 0, "failed"
    with system_session("worker: save preview result") as db:
        v = db.get(ContentVersion, uuid.UUID(version_id))
        v.preview_status = status
        v.preview_pages = pages
        org_id = v.org_id
    events.publish(events.org_topic(org_id), kind="content", version_id=version_id)


def process_logo(file_id: str, **_) -> None:
    """Re-encodes the image (drops metadata and any trailing payload) and resizes it."""
    with system_session("worker: process logo") as db:
        f = db.get(StoredFile, uuid.UUID(file_id))
        if not f:
            return
        if f.scan_status == "pending":
            raise Retry(10)
        src = storage.absolute(f.storage_path)
        try:
            with Image.open(src) as im:
                im.verify()
            with Image.open(src) as im:
                im = im.convert("RGBA")
                im.thumbnail((512, 512))
                dest = src.with_suffix(".png")
                im.save(dest, format="PNG")
            if dest != src:
                src.unlink(missing_ok=True)
                f.storage_path = str(Path(f.storage_path).with_suffix(".png"))
            f.mime = "image/png"
            f.size = dest.stat().st_size
        except Exception:
            log.exception("invalid logo %s", file_id)
            f.scan_status = "infected"
            src.unlink(missing_ok=True)
            b = db.get(OrgBranding, f.org_id)
            if b and b.logo_file_id == f.id:
                b.logo_file_id = None
        org_id = f.org_id
    events.publish(events.org_topic(org_id), kind="branding")


# ------------------------------------------------------------ periodic

def refresh_all_momentum(**_) -> None:
    with system_session("worker: nightly momentum refresh") as db:
        rows = db.execute(select(Membership.org_id, Membership.user_id).join(Organization, Organization.id == Membership.org_id)
                          .where(Membership.status == "active", Organization.status == "active")).all()
        for org_id, user_id in rows:
            refresh_momentum(db, org_id, user_id)


def escalate_questions(**_) -> None:
    """Unanswered questions move one level up the hierarchy, then to admins."""
    hours = get_settings().question_escalate_hours
    cutoff = _now() - timedelta(hours=hours)
    with system_session("worker: escalate unanswered questions") as db:
        threads = list(db.scalars(select(Thread).where(Thread.is_question.is_(True), Thread.status == "open",
                                                       Thread.last_activity_at < cutoff, Thread.escalation_level < 5)))
        for t in threads:
            author = db.scalar(select(Membership).where(Membership.org_id == t.org_id, Membership.user_id == t.author_id))
            if not author:
                continue
            chain = upline_chain(db, t.org_id, author.path)
            level = t.escalation_level + 1
            if level < len(chain):
                target = chain[level].user_id
            else:
                target = db.scalar(select(Membership.user_id).where(
                    Membership.org_id == t.org_id, Membership.role.in_(["owner", "admin"]),
                    Membership.status == "active", Membership.user_id != t.author_id).order_by(Membership.role.desc()))
            if not target or target == t.assigned_to:
                t.escalation_level = 5
                continue
            t.assigned_to = target
            t.escalation_level = level
            t.last_activity_at = _now()
            author_name = db.scalar(select(User.name).where(User.id == t.author_id))
            notify(db, None, target, "question_escalated",
                   f"Unanswered for {hours}h — {author_name} asked: {t.title[:100]}",
                   route=f"/questions/{t.id}", org_id=t.org_id)
            audit.record(db, None, "qa.escalated", "thread", t.id, org_id=t.org_id, level=level)


def purge_expired_tokens(**_) -> None:
    with system_session("worker: purge expired auth tokens") as db:
        db.execute(delete(AuthToken).where(AuthToken.expires_at < _now() - timedelta(days=30)))


def prospect_retention(**_) -> None:
    from ..services.prospects import apply_retention

    log.info("prospect retention: %s", apply_retention())


def purge_exports(**_) -> None:
    import time as _t

    d = get_settings().data_dir / "exports"
    if d.exists():
        for f in d.glob("*.csv"):
            if _t.time() - f.stat().st_mtime > 900:
                f.unlink(missing_ok=True)
    # Uploads that were never used (e.g. an import that was cancelled after preview).
    inc = get_settings().upload_tmp_dir
    if inc.exists():
        for f in inc.rglob("*"):
            if f.is_file() and _t.time() - f.stat().st_mtime > 3600:
                f.unlink(missing_ok=True)


TASKS = {
    "prospect_retention": prospect_retention,
    "purge_exports": purge_exports,
    "scan_file": scan_file,
    "convert_document": convert_document,
    "process_logo": process_logo,
    "refresh_all_momentum": refresh_all_momentum,
    "escalate_questions": escalate_questions,
    "purge_expired_tokens": purge_expired_tokens,
}

PERIODIC = [
    ("escalate_questions", timedelta(hours=1)),
    ("refresh_all_momentum", timedelta(hours=6)),
    ("purge_expired_tokens", timedelta(days=1)),
    ("prospect_retention", timedelta(days=1)),
    ("purge_exports", timedelta(minutes=15)),
]
