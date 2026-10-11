"""Per-browser-session state and helpers shared by all views."""

from __future__ import annotations

import asyncio
import functools
import logging
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import flet as ft

from .. import events
from ..config import get_settings
from ..context import NotFound, PermissionDenied, TenantContext, ValidationError
from ..services import auth, content, orgs

log = logging.getLogger("teampath.ui")
PREF_TOKEN = "tp.session"
PREF_ORG = "tp.org"
USER_ERRORS = (ValidationError, PermissionDenied, NotFound)


class Abort(Exception):
    """Stops an action after the user has already been shown a message."""


class AppState:
    def __init__(self, page: ft.Page):
        self.page = page
        self.settings = get_settings()
        self.user: auth.SignedInUser | None = None
        self.ctx: TenantContext | None = None
        self.profile: orgs.OrgProfile | None = None
        self.logo_url: str | None = None
        self.team_logo_url: str | None = None
        self.token: str | None = None  # server-side session token (persisted only with "remember me")
        self.pending_mfa: auth.SignedInUser | None = None
        self.pending_remember = False
        self.signed_in_at = 0.0
        self.last_seen = time.time()
        self.upload_prefix = "u/" + secrets.token_hex(12)
        self.prefs: ft.SharedPreferences | None = None
        self.file_picker: ft.FilePicker | None = None
        self._upload_done: dict[str, asyncio.Future] = {}
        self.refresh: Callable[[], Awaitable[None]] | None = None  # set by the router
        self.live_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._last_live = 0.0
        self.sub_key = id(self)
        self.after_login_route: str | None = None  # deep link to return to after sign-in

    # ------------------------------------------------------------ identity
    @property
    def ip(self) -> str | None:
        return self.page.client_ip

    @property
    def is_wide(self) -> bool:
        return (self.page.width or 1200) >= 900

    @property
    def role(self) -> str:
        return self.ctx.role if self.ctx else ""

    def has_team(self) -> bool:
        return bool(self.ctx and (self.ctx.is_author or self.extra("has_downline")))

    def extra(self, key, default=None):
        return self.ctx.extra.get(key, default) if self.ctx else default

    def touch(self) -> None:
        self.last_seen = time.time()

    def session_expired(self) -> bool:
        s = self.settings
        if not self.user:
            return False
        idle = time.time() - self.last_seen > s.session_idle_minutes * 60
        too_long = not self.pending_remember and time.time() - self.signed_in_at > s.session_max_hours * 3600
        return idle or too_long

    # ------------------------------------------------------------ running services
    async def run(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Runs a (blocking) service call in a worker thread. Service errors meant
        for the user are shown as a message and re-raised as Abort."""
        self.touch()
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except USER_ERRORS as e:
            self.toast(str(e), error=True)
            raise Abort() from e

    def toast(self, message: str, error: bool = False) -> None:
        self.page.show_dialog(ft.SnackBar(
            ft.Text(message, color=ft.Colors.ON_ERROR_CONTAINER if error else None),
            bgcolor=ft.Colors.ERROR_CONTAINER if error else None,
            behavior=ft.SnackBarBehavior.FLOATING, show_close_icon=True,
            duration=ft.Duration(seconds=6 if error else 3),
        ))

    def go(self, route: str) -> None:
        if (self.page.route or "/") == route and self.refresh:
            refresh = self.refresh

            async def again():
                await refresh()

            self.page.run_task(again)
        else:
            self.page.navigate(route)

    # ------------------------------------------------------------ sign-in lifecycle
    async def finish_sign_in(self, user: auth.SignedInUser, remember: bool) -> None:
        self.user = user
        self.pending_mfa = None
        self.pending_remember = remember
        self.signed_in_at = time.time()
        self.token = await self.run(auth.create_session_token, user.id, remember, self.page.client_user_agent, self.ip)
        if remember and self.prefs:
            await self.prefs.set(PREF_TOKEN, self.token)
        events.subscribe(events.user_topic(user.id), self.sub_key, self._on_event)
        await self.choose_org_automatically()

    async def choose_org_automatically(self) -> None:
        choices = await self.run(auth.my_organizations, self.user.id)
        active = [c for c in choices if c.org_status == "active"]
        last = await self.prefs.get(PREF_ORG) if self.prefs else None
        pick = next((c for c in active if str(c.org_id) == last), None)
        if pick is None and len(active) == 1 and len(choices) == 1:
            pick = active[0]
        if pick:
            await self.open_org(pick.org_id)
        else:
            self.go("/orgs")

    async def open_org(self, org_id: uuid.UUID) -> None:
        ctx = await self.run(auth.open_organization, self.user, org_id, self.ip)
        has_downline = await self.run(lambda: any(p.path != ctx.path and p.role != "assistant"
                                                   for p in orgs.list_people(ctx)))
        mfa_needed = await self.run(auth.mfa_required_for, ctx)
        ctx.extra["has_downline"] = has_downline
        ctx.extra["mfa_needed"] = mfa_needed and not self.user.mfa_enabled
        if self.ctx:
            events.unsubscribe_all(self.sub_key)
            events.subscribe(events.user_topic(self.user.id), self.sub_key, self._on_event)
        self.ctx = ctx
        events.subscribe(events.org_topic(ctx.org_id), self.sub_key, self._on_event)
        if self.prefs:
            await self.prefs.set(PREF_ORG, str(org_id))
        await self.load_branding()
        target = self.after_login_route or "/"
        self.after_login_route = None
        self.go("/security" if ctx.extra["mfa_needed"] else target)

    async def load_branding(self) -> None:
        self.profile = await self.run(orgs.get_profile, self.ctx)
        self.logo_url = await self.run(content.file_url, self.ctx, self.profile.logo_file_id, "in")
        self.team_logo_url = await self.run(content.file_url, self.ctx, self.profile.team_logo_file_id, "in")
        from .theme import apply_theme

        apply_theme(self.page, self.profile.primary_color)
        self.page.title = f"{self.profile.team_name or self.profile.name} · {self.settings.app_name}"

    async def sign_out(self, message: str | None = None) -> None:
        if self.token:
            await asyncio.to_thread(auth.revoke_session_token, self.token)
        if self.prefs:
            await self.prefs.remove(PREF_TOKEN)
        events.unsubscribe_all(self.sub_key)
        self.user = self.ctx = self.profile = self.token = None
        self.logo_url = self.team_logo_url = None
        from .theme import apply_theme

        apply_theme(self.page, None)
        self.go("/login")
        if message:
            self.toast(message)

    async def try_resume(self) -> bool:
        if not self.prefs:
            return False
        raw = await self.prefs.get(PREF_TOKEN)
        if not raw:
            return False
        user = await asyncio.to_thread(auth.resume_session, raw)
        if not user:
            await self.prefs.remove(PREF_TOKEN)
            return False
        self.user, self.token, self.pending_remember = user, raw, True
        self.signed_in_at = time.time()
        events.subscribe(events.user_topic(user.id), self.sub_key, self._on_event)
        return True

    async def check_token(self) -> bool:
        """Revoked from another device (e.g. "sign out everywhere")?"""
        if not self.token:
            return False
        return await asyncio.to_thread(auth.session_token_valid, self.token)

    # ------------------------------------------------------------ live updates
    def _on_event(self, event: dict) -> None:
        # Called from whichever thread published the event.
        try:
            self.page.run_task(self._handle_event, event)
        except Exception:
            pass

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("kind", "")
        if kind == "notification" and "notification" in self.live_handlers:
            await self.live_handlers["notification"](event)
        if kind == "branding" and self.ctx:
            await self.load_branding()
        handler = self.live_handlers.get(kind) or self.live_handlers.get("*")
        if handler and time.time() - self._last_live > 1.5:
            self._last_live = time.time()
            await handler(event)

    # ------------------------------------------------------------ uploads
    async def pick_and_upload(self, extensions: list[str], label: str = "Choose a file") -> tuple[Path, str] | None:
        """Lets the user pick one file, uploads it through the signed upload endpoint
        and returns (local temporary path, original file name)."""
        files = await self.file_picker.pick_files(dialog_title=label, file_type=ft.FilePickerFileType.CUSTOM,
                                                  allowed_extensions=extensions, allow_multiple=False)
        if not files:
            return None
        f = files[0]
        if f.size > self.settings.max_upload_mb * 1024 * 1024:
            self.toast(f"That file is larger than {self.settings.max_upload_mb} MB.", error=True)
            return None
        safe = "".join(ch for ch in f.name if ch.isalnum() or ch in "._- ")[-120:] or "upload"
        rel = f"{self.upload_prefix}/{secrets.token_hex(6)}_{safe}"
        fut = asyncio.get_running_loop().create_future()
        self._upload_done[f.name] = fut
        await self.file_picker.upload([ft.FilePickerUploadFile(upload_url=self.page.get_upload_url(rel, 600), id=f.id)])
        try:
            err = await asyncio.wait_for(fut, timeout=900)
        finally:
            self._upload_done.pop(f.name, None)
        if err:
            self.toast(f"Upload failed: {err}", error=True)
            return None
        return self.settings.upload_tmp_dir.resolve() / rel, f.name

    def on_upload(self, e: ft.FilePickerUploadEvent) -> None:
        fut = self._upload_done.get(e.file_name)
        if not fut or fut.done():
            return
        if e.error:
            fut.set_result(e.error)
        elif e.progress is not None and e.progress >= 1.0:
            fut.set_result(None)


def action(fn: Callable[..., Awaitable[Any]]):
    """Decorator for event handlers: user-facing errors become messages, and
    unexpected errors are logged without leaking details to the browser."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        global INFLIGHT
        INFLIGHT += 1
        try:
            return await fn(*args, **kwargs)
        except Abort:
            return False
        except USER_ERRORS as e:
            S = _find_state(args)
            if S:
                S.toast(str(e), error=True)
            return False
        except Exception:
            log.exception("UI action failed")
            S = _find_state(args)
            if S:
                S.toast("Something went wrong. Please try again.", error=True)
            return False
        finally:
            INFLIGHT -= 1

    return wrapper


_STATES: dict[int, AppState] = {}
INFLIGHT = 0  # number of running UI actions (used by tests to wait for completion)


def register_state(S: AppState) -> None:
    _STATES[id(S.page)] = S


def unregister_state(S: AppState) -> None:
    _STATES.pop(id(S.page), None)


def _find_state(args) -> AppState | None:
    for a in args:
        if isinstance(a, AppState):
            return a
        page = getattr(getattr(a, "control", None), "page", None)
        if page is not None and id(page) in _STATES:
            return _STATES[id(page)]
    try:
        return _STATES.get(id(ft.context.page))
    except Exception:
        return None
