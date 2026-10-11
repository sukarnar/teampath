"""ASGI entry point: FastAPI host with the Flet UI mounted at /.

Run: uvicorn teampath.web.main:app --host 0.0.0.0 --port 8000 --proxy-headers
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import flet_web.fastapi as flet_fastapi
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from .. import events
from ..config import get_settings
from ..db import engine
from ..services import auth
from ..ui.app import main as flet_main
from .files import router as files_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()
settings.files_dir.mkdir(parents=True, exist_ok=True)
settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self' https://fonts.gstatic.com; "
    "worker-src 'self' blob:; media-src 'self' blob:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "strict-origin-when-cross-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        if "access-control-allow-origin" in h:
            del h["access-control-allow-origin"]  # Flet adds "*"; this app is same-origin only
        if settings.csp_mode == "enforce":
            h["Content-Security-Policy"] = CSP
        elif settings.csp_mode == "report":
            h["Content-Security-Policy-Report-Only"] = CSP
        if settings.is_production:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


@asynccontextmanager
async def lifespan(_app):
    from ..migrate import ensure_schema

    ensure_schema()  # upgrades (development) or stops with a clear message if the database is behind
    events.start_listener()  # live updates from the worker (previews ready, escalations)
    yield


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SecurityHeaders)
app.include_router(files_router)


@app.get("/health")
def health():
    with engine().connect() as c:
        c.execute(text("select 1"))
    return JSONResponse({"status": "ok"})


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta name=viewport content='width=device-width'><title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;color:#222}"
        "a{color:#3355cc}</style></head>"
        f"<body><h2>{title}</h2><p>{body}</p><p><a href='/login'>Go to sign in</a></p></body></html>"
    )


@app.put("/upload")
async def upload(request: Request):
    """Signed uploads from the Flet FilePicker (URLs come from page.get_upload_url)."""
    await flet_fastapi.FletUpload(
        upload_dir=str(settings.upload_tmp_dir.resolve()),
        max_upload_size=settings.max_upload_mb * 1024 * 1024,
        secret_key=settings.flet_secret_key,
    ).handle(request)
    return JSONResponse({"ok": True})


@app.get("/verify-email")
def verify_email(token: str = ""):
    if auth.verify_email(token):
        return _page("Email confirmed", "Thanks — your email is confirmed. You can sign in now.")
    return _page("Link expired", "This confirmation link is invalid or has expired.")


app.mount(
    "/",
    flet_fastapi.app(
        flet_main,
        app_name=settings.app_name,
        app_short_name=settings.app_name,
        app_description=f"{settings.app_name} — onboarding, goals and team progress",
        upload_endpoint_path="/upload",  # served by the host route above
        secret_key=settings.flet_secret_key,
        session_timeout_seconds=60 * 15,
        no_cdn=True,
        assets_dir=str((Path(__file__).parents[1] / "ui" / "assets").resolve()),
    ),
)
