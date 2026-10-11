"""Account security: two-step verification, password and active sessions."""

from __future__ import annotations

import flet as ft

from ...security import qr_png_base64
from ...services import auth
from .. import components as C
from ..state import AppState, action


async def security_view(S: AppState, q: dict) -> ft.Control:
    forced = bool(S.extra("mfa_needed"))
    sessions = await S.run(auth.list_sessions, S.user.id)
    blocks: list[ft.Control] = []

    if forced:
        blocks.append(ft.Container(ft.Row([
            ft.Icon(ft.Icons.SHIELD_OUTLINED, color=ft.Colors.ON_TERTIARY_CONTAINER),
            ft.Text("Your role requires two-step verification. Set it up to continue.", expand=True,
                    color=ft.Colors.ON_TERTIARY_CONTAINER, weight=ft.FontWeight.W_500),
        ]), bgcolor=ft.Colors.TERTIARY_CONTAINER, padding=14, border_radius=12))

    # ---------------------------------------------------- MFA
    mfa_box = ft.Column(spacing=12)

    def render_mfa():
        mfa_box.controls.clear()
        if S.user.mfa_enabled:
            pw = C.text_field("Current password", password=True)
            code = C.text_field("Code from your app", max_length=6, keyboard_type=ft.KeyboardType.NUMBER)

            @action
            async def disable(e):
                ok = await S.run(auth.disable_mfa, S.user.id, pw.value, code.value)
                if not ok:
                    S.toast("Password or code is incorrect.", error=True)
                    return False
                S.user.mfa_enabled = False
                S.toast("Two-step verification turned off.")
                await S.refresh()

            mfa_box.controls += [
                ft.Row([ft.Icon(ft.Icons.VERIFIED_USER, color=ft.Colors.PRIMARY),
                        ft.Text("Two-step verification is on.", weight=ft.FontWeight.W_500)]),
                ft.OutlinedButton("Turn off", on_click=lambda e: C.form_dialog(
                    S.page, "Turn off two-step verification", [pw, code], disable, "Turn off")),
            ]
        else:
            mfa_box.controls += [
                ft.Text("Protect your account with a code from an authenticator app "
                        "(Google Authenticator, Microsoft Authenticator, 1Password…)."),
                ft.FilledButton("Set up two-step verification", icon=ft.Icons.QR_CODE_2, on_click=start_setup),
            ]

    @action
    async def start_setup(e):
        secret, uri = auth.begin_mfa_setup(S.user)
        code = C.text_field("6-digit code", keyboard_type=ft.KeyboardType.NUMBER, max_length=6, autofocus=True)

        async def confirm():
            ok = await S.run(auth.confirm_mfa_setup, S.user.id, secret, code.value)
            if not ok:
                S.toast("That code didn't match. Check your phone's time and try again.", error=True)
                return False
            S.user.mfa_enabled = True
            if S.ctx:
                S.ctx.extra["mfa_needed"] = False
            S.toast("Two-step verification is on.")
            if forced:
                S.go("/")
            else:
                await S.refresh()

        C.form_dialog(S.page, "Scan with your authenticator app", [
            ft.Image(src=f"data:image/png;base64,{qr_png_base64(uri)}", width=200, height=200),
            ft.Text("Can't scan? Enter this key:", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), selectable=True,
                    font_family="monospace", weight=ft.FontWeight.W_600),
            code,
        ], action(confirm), "Turn on", width=380)

    render_mfa()
    blocks.append(C.card(C.section("Two-step verification", mfa_box)))

    if not forced:
        # ------------------------------------------------ password
        cur = C.text_field("Current password", password=True)
        new = C.text_field("New password", password=True, can_reveal_password=True,
                           helper=f"At least {S.settings.password_min_length} characters")

        @action
        async def change(e):
            await S.run(auth.change_password, S.user.id, cur.value, new.value, S.token)
            cur.value = new.value = ""
            S.toast("Password changed. Other devices were signed out.")

        blocks.append(C.card(C.section("Password", cur, new, ft.Row([ft.FilledButton("Change password", on_click=change)]))))

        # ------------------------------------------------ sessions
        @action
        async def revoke(e):
            await S.run(auth.revoke_session, S.user.id, e.control.data)
            await S.refresh()

        @action
        async def revoke_all(e):
            async def yes():
                await S.run(auth.revoke_all_sessions, S.user.id, S.token)
                S.toast("Signed out everywhere else.")
                await S.refresh()

            C.confirm(S.page, "Sign out other devices?", "Every other browser and phone will need to sign in again.",
                      yes, "Sign out others")

        from ...security import token_hash

        mine = token_hash(S.token) if S.token else ""
        rows = []
        for t in sessions:
            current = t.token_hash == mine
            rows.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.DEVICES_OUTLINED),
                title=ft.Text(_device(t.user_agent) + (" · this device" if current else "")),
                subtitle=ft.Text(f"Last active {C.when(t.last_seen_at)} · {t.ip or 'unknown IP'}"),
                trailing=None if current else ft.TextButton("Sign out", data=t.id, on_click=revoke),
            ))
        blocks.append(C.card(C.section("Where you're signed in", *rows,
                                       trailing=ft.TextButton("Sign out all others", on_click=revoke_all))))

    return C.page_body(*blocks, title="Security", subtitle=S.user.email)


def _device(ua: str | None) -> str:
    ua = ua or ""
    for key, label in (("iPhone", "iPhone"), ("Android", "Android"), ("iPad", "iPad"), ("Macintosh", "Mac"),
                       ("Windows", "Windows"), ("Linux", "Linux")):
        if key in ua:
            browser = next((b for b in ("Edg", "Chrome", "Firefox", "Safari") if b in ua), "Browser")
            return f"{label} · {'Edge' if browser == 'Edg' else browser}"
    return "Browser"
