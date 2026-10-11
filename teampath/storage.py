"""File storage on the local data volume, partitioned by organization."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from pathlib import Path

from .config import get_settings
from .context import ValidationError

ALLOWED = {
    # extension: (expected mime, magic-bytes check)
    ".pdf": ("application/pdf", lambda b: b.startswith(b"%PDF")),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", lambda b: b.startswith(b"PK")),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", lambda b: b.startswith(b"PK")),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", lambda b: b.startswith(b"PK")),
    ".png": ("image/png", lambda b: b.startswith(b"\x89PNG")),
    ".jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8")),
    ".jpeg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8")),
    ".mp4": ("video/mp4", lambda b: b[4:8] == b"ftyp"),
    ".txt": ("text/plain", lambda b: b"\x00" not in b),
    ".md": ("text/markdown", lambda b: b"\x00" not in b),
}
LOGO_EXT = {".png", ".jpg", ".jpeg"}
CONVERTIBLE = {".pptx", ".docx", ".xlsx"}


def validate_upload(path: Path, original_name: str, allowed: set[str] | None = None) -> tuple[str, str]:
    """Checks extension, size and content signature. Returns (ext, mime)."""
    ext = Path(original_name).suffix.lower()
    allowed = allowed or set(ALLOWED)
    if ext not in allowed or ext not in ALLOWED:
        raise ValidationError(f"File type {ext or '(none)'} is not allowed.")
    size = path.stat().st_size
    if size == 0:
        raise ValidationError("The file is empty.")
    if size > get_settings().max_upload_mb * 1024 * 1024:
        raise ValidationError("The file is too large.")
    with open(path, "rb") as f:
        head = f.read(4096)
    mime, check = ALLOWED[ext]
    if not check(head):
        raise ValidationError("The file content doesn't match its extension.")
    return ext, mime


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def store(src: Path, org_id, file_id: uuid.UUID, ext: str) -> str:
    """Moves an uploaded file into /data/files/<org_id>/<file_id><ext>; returns relative path."""
    rel = Path(str(org_id)) / f"{file_id}{ext}"
    dest = get_settings().files_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), dest)
    dest.chmod(0o640)
    return str(rel)


def absolute(rel: str) -> Path:
    base = get_settings().files_dir.resolve()
    p = (base / rel).resolve()
    if base not in p.parents:
        raise ValidationError("Invalid storage path")
    return p


def preview_dir(rel: str) -> Path:
    p = absolute(rel)
    return p.with_name(p.stem + "_pages")


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
