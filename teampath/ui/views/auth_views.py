"""Public screens: sign in, MFA, sign up, password reset, invitations; and the org picker."""

from __future__ import annotations

import asyncio

import flet as ft

from ...services import auth
from .. import components as C
from ..state import AppState, action


def frame(S: AppState, title: str, subtitle: str, fields: list[ft.Control], footer: list[ft.Control] | None = None,
          accent: str | None = None) -> ft.Control:
    """Centered card used by all public screens."""
    brand = ft.Row([
        ft.Container(ft.Icon(ft.Icons.ROUTE, color=ft.Colors.ON_PRIMARY, size=20), bgcolor=accent or ft.Colors.PRIMARY,
                     border_radius=10, padding=8),
        ft.Text(S.settings.app_name, size=20, weight=ft.FontWeight.W_700),
    ], spacing=10)
    body = ft.Column([
        brand,
        ft.Container(height=8),
        ft.Text(title, theme_style=ft.TextThemeStyle.HEADLINE_SMALL, weight=ft.FontWeight.W_600),
        ft.Text(subtitle, color=ft.Colors.ON_SURFACE_VARIANT),
        ft.Container(height=4),
        *fields,
        *(footer or []),
    ], spacing=12, tight=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
    return ft.Container(
        ft.Container(body, width=420, padding=28, border_radius=16, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                     border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)),
        alignment=ft.Alignment.CENTER, expand=True, padding=16,
    )


def _error_text() -> ft.Text:
    return ft.Text("", color=ft.Colors.ERROR, visible=False)


def _show(err: ft.Text, message: str) -> None:
    err.value = message
    err.visible = bool(message)


async def login_view(S: AppState, q: dict) -> ft.Control:
    if S.user and S.ctx:
        S.go("/")
        return None
    email = C.text_field("Email", autofill_hints=[ft.AutofillHint.EMAIL], keyboard_type=ft.KeyboardType.EMAIL,
                         autofocus=True)
    password = C.text_field("Password", password=True, can_reveal_password=True,
                            autofill_hints=[ft.AutofillHint.PASSWORD])
    remember = ft.Checkbox(label="Keep me signed in on this device", value=False)
    err = _error_text()
    btn = ft.FilledButton("Sign in", width=float("inf"), height=44)

    @action
    async def submit(e):
        _show(err, "")
        btn.disabled = True
        btn.update()
        try:
            r = await S.run(auth.login, email.value, password.value, S.ip)
        finally:
            btn.disabled = False
        if r.status == "ok":
            await S.finish_sign_in(r.user, remember.value)
        elif r.status == "mfa_required":
            S.pending_mfa, S.pending_remember = r.user, remember.value
            S.go("/mfa")
        else:
            password.value = ""
            _show(err, r.message)

    btn.on_click = submit
    password.on_submit = submit
    return frame(S, "Welcome back", "Sign in to continue.", [
        email, password, remember, err, btn,
        ft.Row([ft.TextButton("Forgot password?", on_click=lambda e: S.go("/forgot")),
                ft.TextButton("Create an organization", on_click=lambda e: S.go("/signup"))],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
    ])


async def mfa_view(S: AppState, q: dict) -> ft.Control:
    if not S.pending_mfa:
        S.go("/login")
        return None
    code = C.text_field("6-digit code", keyboard_type=ft.KeyboardType.NUMBER, max_length=6, autofocus=True,
                        autofill_hints=[ft.AutofillHint.ONE_TIME_CODE])
    err = _error_text()

    @action
    async def submit(e):
        r = await S.run(auth.verify_login_mfa, S.pending_mfa.id, code.value, S.ip)
        if r.status == "ok":
            await S.finish_sign_in(r.user, S.pending_remember)
        else:
            code.value = ""
            _show(err, r.message)
            if r.status == "locked":
                S.pending_mfa = None

    code.on_submit = submit
    return frame(S, "Two-step verification", "Enter the code from your authenticator app.", [
        code, err, ft.FilledButton("Verify", on_click=submit, width=float("inf"), height=44),
        ft.TextButton("Back to sign in", on_click=lambda e: S.go("/login")),
    ])


async def signup_view(S: AppState, q: dict) -> ft.Control:
    name = C.text_field("Your name", autofill_hints=[ft.AutofillHint.NAME])
    email = C.text_field("Work email", keyboard_type=ft.KeyboardType.EMAIL, autofill_hints=[ft.AutofillHint.EMAIL])
    password = C.text_field("Password", password=True, can_reveal_password=True,
                            helper=f"At least {S.settings.password_min_length} characters",
                            autofill_hints=[ft.AutofillHint.NEW_PASSWORD])
    org_name = C.text_field("Organization or agency name", autofill_hints=[ft.AutofillHint.ORGANIZATION_NAME])
    org_type = ft.Dropdown(label="Type", value="insurance", border_radius=10, options=[
        ft.DropdownOption(key="insurance", text="Insurance agency (teams, licensing, daily activity)"),
        ft.DropdownOption(key="generic", text="Any team or company"),
    ])
    err = _error_text()

    @action
    async def submit(e):
        _show(err, "")
        await S.run(auth.signup_organization, name=name.value, email=email.value, password=password.value,
                    org_name=org_name.value, org_type=org_type.value, ip=S.ip)
        msgs = []
        if S.settings.smtp_host:
            msgs.append("Check your email to confirm your address.")
        if S.settings.require_org_approval:
            msgs.append("Your organization will be available once it's approved.")
        S.go("/login")
        S.toast(" ".join(msgs) or "Account created. Sign in to continue.")

    return frame(S, "Create your organization", "You'll be the owner. You can invite your team next.", [
        name, email, password, org_name, org_type, err,
        ft.FilledButton("Create organization", on_click=submit, width=float("inf"), height=44),
        ft.TextButton("I already have an account", on_click=lambda e: S.go("/login")),
    ])


async def forgot_view(S: AppState, q: dict) -> ft.Control:
    email = C.text_field("Email", keyboard_type=ft.KeyboardType.EMAIL)
    done = ft.Text("", visible=False)

    @action
    async def submit(e):
        await S.run(auth.request_password_reset, email.value)
        done.value = "If an account exists for that email, we've sent a reset link. It expires in an hour."
        done.visible = True

    return frame(S, "Reset your password", "We'll email you a link.", [
        email, done, ft.FilledButton("Send reset link", on_click=submit, width=float("inf"), height=44),
        ft.TextButton("Back to sign in", on_click=lambda e: S.go("/login")),
    ])


async def reset_view(S: AppState, q: dict) -> ft.Control:
    token = q.get("token", "")
    pw = C.text_field("New password", password=True, can_reveal_password=True,
                      helper=f"At least {S.settings.password_min_length} characters")
    pw2 = C.text_field("Confirm new password", password=True)
    err = _error_text()

    @action
    async def submit(e):
        if pw.value != pw2.value:
            _show(err, "The passwords don't match.")
            return
        await S.run(auth.reset_password, token, pw.value)
        S.go("/login")
        S.toast("Password changed. Sign in with your new password.")

    return frame(S, "Choose a new password", "This signs you out on all other devices.", [
        pw, pw2, err, ft.FilledButton("Save password", on_click=submit, width=float("inf"), height=44)])


async def invite_view(S: AppState, q: dict) -> ft.Control:
    token = q.get("token", "")
    info = await asyncio.to_thread(auth.inspect_invitation, token)
    if not info:
        return frame(S, "Invitation expired", "Ask the person who invited you to send a new invitation.",
                     [ft.TextButton("Go to sign in", on_click=lambda e: S.go("/login"))])
    err = _error_text()
    name = C.text_field("Your name", autofill_hints=[ft.AutofillHint.NAME])
    pw = C.text_field("Choose a password" if not info.has_account else "Your current password", password=True,
                      can_reveal_password=True,
                      helper=None if info.has_account else f"At least {S.settings.password_min_length} characters")

    @action
    async def submit(e):
        if info.has_account:
            await S.run(auth.accept_invitation, token, existing_password=pw.value, ip=S.ip)
        else:
            await S.run(auth.accept_invitation, token, name=name.value, password=pw.value, ip=S.ip)
        r = await S.run(auth.login, info.email, pw.value, S.ip)
        if r.status == "ok":
            await S.finish_sign_in(r.user, False)
        elif r.status == "mfa_required":
            S.pending_mfa = r.user
            S.go("/mfa")
        else:
            S.go("/login")

    fields = [C.text_field("Email", value=info.email, read_only=True)]
    if not info.has_account:
        fields.append(name)
    fields += [pw, err, ft.FilledButton("Join " + info.org_name, on_click=submit, width=float("inf"), height=44)]
    return frame(S, f"Join {info.org_name}", f"You've been invited as {info.role}.", fields, accent=info.primary_color)


async def orgs_view(S: AppState, q: dict) -> ft.Control:
    choices = await S.run(auth.my_organizations, S.user.id)

    @action
    async def open_org(e):
        await S.open_org(e.control.data)

    tiles = []
    for c in choices:
        status = {"pending": "Waiting for approval", "suspended": "Suspended"}.get(c.org_status)
        tiles.append(C.card(ft.ListTile(
            leading=C.avatar(c.org_name), title=ft.Text(c.org_name, weight=ft.FontWeight.W_600),
            subtitle=ft.Text(status or c.role.title()),
            trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT) if not status else None,
            data=c.org_id, on_click=open_org if not status else None, disabled=bool(status)), padding=4))
    return C.page_body(*(tiles or [C.empty("You're not a member of any organization yet.")]),
                       title="Choose an organization", subtitle=f"Signed in as {S.user.email}")
