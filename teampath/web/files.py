"""Serves stored files through short-lived signed URLs (see security.sign_file_url)."""

from __future__ import annotations

import io
import uuid
from datetime import date

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from .. import audit, previews, storage
from ..db import system_session
from ..models import Membership, StoredFile, User
from ..security import verify_file_signature

router = APIRouter()


def _watermark(path, text: str) -> bytes:
    with Image.open(path) as im:
        im = im.convert("RGBA")
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        size = max(14, im.size[0] // 45)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            font = ImageFont.load_default(size=size)
        step_y = size * 9
        for i, y in enumerate(range(size, im.size[1], step_y)):
            x = (i % 2) * im.size[0] // 3 + size
            draw.text((x, y), text, fill=(120, 120, 120, 70), font=font)
        out = Image.alpha_composite(im, layer).convert("RGB")
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


@router.get("/files/{file_id}")
def get_file(file_id: uuid.UUID, u: uuid.UUID, v: str, e: int, s: str):
    if not verify_file_signature(str(file_id), str(u), v, e, s):
        raise HTTPException(status_code=403, detail="Link expired")
    with system_session("files: serve signed file") as db:
        f = db.get(StoredFile, file_id)
        user = db.get(User, u)
        # Defense in depth: the signer must still be an active member of the file's organization.
        member = db.scalar(select(Membership.id).where(Membership.org_id == (f.org_id if f else None),
                                                       Membership.user_id == u, Membership.status == "active"))
        if not f or not user or user.status != "active" or not member or f.scan_status not in ("clean", "skipped"):
            raise HTTPException(status_code=404)
        if v == "orig":
            audit.record(db, None, "file.downloaded", "file", f.id, org_id=f.org_id, actor_id=u)
        path = storage.absolute(f.storage_path)
        name, email = user.name, user.email
    headers = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    if v == "orig":
        inline = f.mime.startswith(("image/", "video/")) or f.mime == "application/pdf"
        disp = "inline" if inline else "attachment"
        return FileResponse(path, media_type=f.mime, filename=f.original_name, content_disposition_type=disp,
                            headers=headers)
    wm = v.endswith("w")
    if v.startswith("p") and v[1:-1].isdigit():
        try:
            page = previews.page_image(f.storage_path, int(v[1:-1]))  # rendered on first view, then cached
        except FileNotFoundError:
            raise HTTPException(status_code=404)
    elif v.startswith("i"):
        page = path
    else:
        raise HTTPException(status_code=400)
    if not page.exists():
        raise HTTPException(status_code=404)
    if wm:
        mark = f"{name} · {email} · {date.today().isoformat()}"
        return Response(_watermark(page, mark), media_type="image/png", headers=headers)
    return FileResponse(page, media_type="image/png", headers=headers)


@router.get("/exports/{token}")
def get_export(token: str, u: uuid.UUID, e: int, s: str):
    """One-time download of an agent's own prospect export (signed for that user, 5 minutes)."""
    from ..services.prospects import verify_export

    path = verify_export(token, str(u), e, s)
    if not path:
        raise HTTPException(status_code=403, detail="Link expired")
    data = path.read_bytes()
    path.unlink(missing_ok=True)  # single use
    return Response(data, media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="my-prospects.csv"', "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"})
