"""Virus scanning and page previews, shared by the web app and the worker.

PDFs are previewable the moment they're uploaded: we only count their pages at
upload time and render each page on first request (then cache it). Office files
(Word, Excel, PowerPoint) still need LibreOffice, which runs in the worker; it
converts them to a PDF once, and pages are then served the same way.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import struct
import subprocess
import tempfile
from pathlib import Path

import pymupdf

from . import storage
from .config import get_settings

log = logging.getLogger("teampath.previews")
MAX_PAGES = 300
PAGE_WIDTH = 1200
CONVERTED_NAME = "converted.pdf"


class ScannerUnavailable(Exception):
    pass


# ------------------------------------------------------------ antivirus

def clamd_scan(path: Path, timeout: float = 60) -> str:
    """Returns 'clean' or 'infected' using clamd's INSTREAM command."""
    s = get_settings()
    try:
        sock = socket.create_connection((s.clamd_host, s.clamd_port), timeout=timeout)
    except OSError as e:
        raise ScannerUnavailable(str(e)) from e
    with sock:
        sock.sendall(b"zINSTREAM\0")
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
        sock.sendall(struct.pack("!L", 0))
        reply = sock.recv(4096).decode(errors="replace").strip("\0 \n")
    if reply.endswith("OK"):
        return "clean"
    if "FOUND" in reply:
        return "infected"
    raise ScannerUnavailable(f"clamd error: {reply}")


def scan_now(path: Path) -> str:
    """Scan status to store for a new upload: 'skipped' when scanning is off,
    'clean'/'infected' when the scanner answers, 'pending' when it is unavailable
    (the worker retries)."""
    if not get_settings().clamd_host:
        return "skipped"
    try:
        return clamd_scan(path, timeout=30)
    except ScannerUnavailable as e:
        log.warning("virus scanner unavailable, will retry in the background: %s", e)
        return "pending"


# ------------------------------------------------------------ PDFs

def pdf_page_count(path: Path) -> int:
    """Number of pages, or raises ValueError for unreadable / password-protected PDFs."""
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise ValueError("unreadable PDF") from e
    try:
        if doc.needs_pass:
            raise ValueError("password-protected PDF")
        return min(doc.page_count, MAX_PAGES)
    finally:
        doc.close()


def preview_source(rel: str) -> Path:
    """The PDF that pages are rendered from: the upload itself, or its converted copy."""
    original = storage.absolute(rel)
    if original.suffix.lower() == ".pdf":
        return original
    return storage.preview_dir(rel) / CONVERTED_NAME


def page_image(rel: str, page_no: int) -> Path:
    """PNG for one page, rendered on first request and cached next to the file."""
    out_dir = storage.preview_dir(rel)
    target = out_dir / f"page_{page_no:03d}.png"
    if target.exists():
        return target
    src = preview_source(rel)
    if not src.exists():
        raise FileNotFoundError(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(src)
    try:
        if page_no < 1 or page_no > min(doc.page_count, MAX_PAGES):
            raise FileNotFoundError(f"page {page_no}")
        page = doc.load_page(page_no - 1)
        zoom = PAGE_WIDTH / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".png")
        os.close(fd)
        pix.save(tmp)
        os.replace(tmp, target)  # atomic: concurrent requests never see half a file
    finally:
        doc.close()
    return target


def clear_cache(rel: str) -> None:
    d = storage.preview_dir(rel)
    if d.exists():
        for p in d.glob("page_*.png"):
            p.unlink(missing_ok=True)


def convert_office_to_pdf(rel: str) -> int:
    """LibreOffice conversion (worker only). Returns the page count."""
    src = storage.absolute(rel)
    out_dir = storage.preview_dir(rel)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # Isolated LibreOffice profile per conversion, time-boxed.
        subprocess.run(
            ["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--norestore",
             "--convert-to", "pdf", "--outdir", tmp, str(src)],
            check=True, timeout=180, capture_output=True,
        )
        produced = Path(tmp) / (src.stem + ".pdf")
        clear_cache(rel)
        shutil.move(str(produced), out_dir / CONVERTED_NAME)
    return pdf_page_count(out_dir / CONVERTED_NAME)
