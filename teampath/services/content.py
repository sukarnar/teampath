"""Content library: documents, slides, videos, links and text, with versions and previews."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select

from .. import audit, jobs, previews, storage
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..db import tenant_session
from ..models import ContentItem, ContentVersion, StoredFile
from ..security import sign_file_url
from .orgs import save_upload

KINDS = ("document", "slides", "video", "link", "text")


@dataclass
class ContentView:
    id: uuid.UUID
    title: str
    description: str | None
    kind: str
    tags: list[str]
    scoped: bool
    allow_download: bool
    watermark: bool
    version_no: int | None
    preview_status: str
    page_urls: list[str] = field(default_factory=list)
    download_url: str | None = None
    link_url: str | None = None
    body_md: str | None = None
    file_name: str | None = None
    can_edit: bool = False
    page_count: int = 0
    is_pdf: bool = False


def _kind_for(ext: str) -> str:
    return {".pptx": "slides", ".mp4": "video"}.get(ext, "document")


def _can_edit(ctx: TenantContext, item: ContentItem) -> bool:
    if ctx.is_admin:
        return True
    return ctx.role == "leader" and item.scope_path is not None and (
        item.scope_path == ctx.path or item.scope_path.startswith(ctx.path + "."))


def create_item(ctx: TenantContext, *, title: str, kind: str, description: str = "", tags: list[str] | None = None,
                url: str | None = None, body_md: str | None = None, tmp_path: Path | None = None,
                original_name: str | None = None, team_only: bool = False, allow_download: bool = True,
                watermark: bool = False) -> uuid.UUID:
    """Admins publish org-wide content; leaders publish content for their own team."""
    ctx.require_author()
    title = (title or "").strip()
    if not title:
        raise ValidationError("Enter a title.")
    if tmp_path:
        kind = _kind_for(Path(original_name or "").suffix.lower())
    if kind not in KINDS:
        raise ValidationError("Unknown content type.")
    if kind == "link" and not (url or "").startswith("https://"):
        raise ValidationError("Links must start with https://")
    if kind == "text" and not (body_md or "").strip():
        raise ValidationError("Write some text.")
    if kind in ("document", "slides", "video") and not tmp_path:
        raise ValidationError("Choose a file to upload.")
    scope = ctx.path if (team_only or not ctx.is_admin) else None
    file_id = save_upload(ctx, tmp_path, original_name, "content") if tmp_path else None
    with tenant_session(ctx) as db:
        item = ContentItem(org_id=ctx.org_id, title=title[:200], description=(description or "").strip() or None,
                           kind=kind, tags=[t.strip().lower()[:40] for t in (tags or []) if t.strip()],
                           scope_path=scope, allow_download=allow_download, watermark=watermark,
                           created_by=ctx.user_id)
        db.add(item)
        db.flush()
        v = _add_version(db, ctx, item, 1, file_id=file_id, url=url, body_md=body_md, original_name=original_name)
        item.current_version_id = v.id
        audit.record(db, ctx, "content.created", "content", item.id, title=title, kind=kind)
        return item.id


def _add_version(db, ctx, item, version_no, *, file_id=None, url=None, body_md=None, original_name=None):
    ext = Path(original_name or "").suffix.lower()
    status, pages = "none", 0
    if file_id and ext == ".pdf":
        f = db.get(StoredFile, file_id)
        if f.scan_status in ("clean", "skipped"):
            # PDFs are ready immediately: count pages now, render each page on first view.
            try:
                pages, status = previews.pdf_page_count(storage.absolute(f.storage_path)), "ready"
            except ValueError:
                status = "failed"
        else:
            status = "pending"  # waiting for the virus scan
    elif file_id and ext in storage.CONVERTIBLE:
        status = "pending"  # Word/Excel/PowerPoint: converted by the worker
    v = ContentVersion(org_id=ctx.org_id, item_id=item.id, version_no=version_no, file_id=file_id,
                       url=url, body_md=body_md, preview_status=status, preview_pages=pages,
                       created_by=ctx.user_id)
    db.add(v)
    db.flush()
    if status == "pending":
        jobs.enqueue(db, "convert_document", version_id=v.id, org_id=ctx.org_id)
    return v


def add_version(ctx: TenantContext, item_id: uuid.UUID, *, tmp_path: Path | None = None, original_name: str | None = None,
                url: str | None = None, body_md: str | None = None) -> None:
    with tenant_session(ctx) as db:
        item = db.get(ContentItem, item_id)
        if not item:
            raise NotFound("Content not found")
        if not _can_edit(ctx, item):
            raise PermissionDenied("You can't edit this content.")
    file_id = save_upload(ctx, tmp_path, original_name, "content") if tmp_path else None
    with tenant_session(ctx) as db:
        item = db.get(ContentItem, item_id)
        n = db.scalar(select(func.max(ContentVersion.version_no)).where(ContentVersion.item_id == item.id)) or 0
        v = _add_version(db, ctx, item, n + 1, file_id=file_id, url=url, body_md=body_md, original_name=original_name)
        item.current_version_id = v.id
        audit.record(db, ctx, "content.new_version", "content", item.id, version=n + 1)


def update_item(ctx: TenantContext, item_id: uuid.UUID, **fields) -> None:
    with tenant_session(ctx) as db:
        item = db.get(ContentItem, item_id)
        if not item:
            raise NotFound("Content not found")
        if not _can_edit(ctx, item):
            raise PermissionDenied("You can't edit this content.")
        for k, v in fields.items():
            if k == "title":
                if not (v or "").strip():
                    raise ValidationError("Enter a title.")
                item.title = v.strip()[:200]
            elif k == "description":
                item.description = (v or "").strip() or None
            elif k == "tags":
                item.tags = [t.strip().lower()[:40] for t in v if t.strip()]
            elif k in ("allow_download", "watermark", "archived"):
                setattr(item, k, bool(v))
            else:
                raise ValidationError(f"Unknown field {k}")
        audit.record(db, ctx, "content.updated", "content", item.id, fields=sorted(fields))


def list_items(ctx: TenantContext, search: str = "", include_archived: bool = False) -> list[ContentView]:
    with tenant_session(ctx) as db:
        q = (select(ContentItem, ContentVersion).outerjoin(ContentVersion, ContentVersion.id == ContentItem.current_version_id)
             .where(ContentItem.org_id == ctx.org_id))
        if not include_archived:
            q = q.where(ContentItem.archived.is_(False))
        if search:
            like = f"%{search.strip()}%"
            q = q.where(ContentItem.title.ilike(like) | ContentItem.description.ilike(like)
                        | func.array_to_string(ContentItem.tags, " ").ilike(like))
        rows = db.execute(q.order_by(ContentItem.created_at.desc())).all()
        return [
            ContentView(i.id, i.title, i.description, i.kind, i.tags or [], i.scope_path is not None, i.allow_download,
                        i.watermark, v.version_no if v else None, v.preview_status if v else "none",
                        can_edit=_can_edit(ctx, i))
            for i, v in rows
        ]


def open_item(ctx: TenantContext, item_id: uuid.UUID) -> ContentView:
    """Returns the item with short-lived, per-user signed URLs for its pages/file."""
    with tenant_session(ctx) as db:
        item = db.get(ContentItem, item_id)
        if not item:
            raise NotFound("Content not found")  # also what RLS returns for out-of-scope items
        v = db.get(ContentVersion, item.current_version_id) if item.current_version_id else None
        f = db.get(StoredFile, v.file_id) if v and v.file_id else None
        view = ContentView(item.id, item.title, item.description, item.kind, item.tags or [],
                           item.scope_path is not None, item.allow_download, item.watermark,
                           v.version_no if v else None, v.preview_status if v else "none",
                           link_url=v.url if v else None, body_md=v.body_md if v else None,
                           file_name=f.original_name if f else None, can_edit=_can_edit(ctx, item))
        if f and f.scan_status in ("clean", "skipped"):
            wm = "w" if item.watermark else "n"
            if v.preview_status == "ready":
                view.page_count = v.preview_pages
                view.page_urls = [sign_file_url(str(f.id), str(ctx.user_id), f"p{n}{wm}", ttl=1800)
                                  for n in range(1, min(v.preview_pages, FIRST_PAGES) + 1)]
            view.is_pdf = f.mime == "application/pdf"
            if item.allow_download or ctx.is_admin or f.mime.startswith("video/"):
                view.download_url = sign_file_url(str(f.id), str(ctx.user_id), "orig", ttl=300)
            if f.mime.startswith("image/"):
                view.page_urls = [sign_file_url(str(f.id), str(ctx.user_id), f"i{wm}", ttl=900)]
        elif f:
            view.preview_status = "scanning" if f.scan_status == "pending" else "blocked"
        audit.record(db, ctx, "content.viewed", "content", item.id, version=view.version_no)
        return view


FIRST_PAGES = 5


def preview_state(ctx: TenantContext, item_id: uuid.UUID) -> str:
    """Cheap status check used while a preview is being prepared."""
    with tenant_session(ctx) as db:
        row = db.execute(select(ContentVersion.preview_status, StoredFile.scan_status)
                         .join(ContentItem, ContentItem.current_version_id == ContentVersion.id)
                         .outerjoin(StoredFile, StoredFile.id == ContentVersion.file_id)
                         .where(ContentItem.id == item_id)).first()
        if not row:
            raise NotFound("Content not found")
        status, scan = row
        return "scanning" if scan == "pending" else status


def page_urls(ctx: TenantContext, item_id: uuid.UUID, start: int, count: int = 10) -> list[str]:
    """Fresh signed URLs for more pages (the viewer loads pages in batches)."""
    with tenant_session(ctx) as db:
        item = db.get(ContentItem, item_id)  # RLS decides visibility
        v = db.get(ContentVersion, item.current_version_id) if item and item.current_version_id else None
        f = db.get(StoredFile, v.file_id) if v and v.file_id else None
        if not f or v.preview_status != "ready" or f.scan_status not in ("clean", "skipped"):
            raise NotFound("Content not found")
        wm = "w" if item.watermark else "n"
        end = min(v.preview_pages, start + count - 1)
        return [sign_file_url(str(f.id), str(ctx.user_id), f"p{n}{wm}", ttl=1800) for n in range(start, end + 1)]


def file_url(ctx: TenantContext, file_id: uuid.UUID | None, variant: str = "orig") -> str | None:
    """Signed URL for a file the caller can see (logos etc.)."""
    if not file_id:
        return None
    with tenant_session(ctx) as db:
        f = db.get(StoredFile, file_id)
        if not f or f.scan_status not in ("clean", "skipped"):
            return None
    return sign_file_url(str(file_id), str(ctx.user_id), variant, ttl=3600)
