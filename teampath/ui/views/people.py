"""People: your upline and downline, invitations, and (admins/leaders) hierarchy management."""

from __future__ import annotations

import asyncio
import uuid

import flet as ft

from ...context import ROLES
from ...services import orgs
from ...storage import LOGO_EXT
from .. import components as C
from ..state import AppState, action

ROLE_LABEL = {"owner": "Owner", "admin": "Admin", "leader": "Leader", "member": "Agent / member",
              "assistant": "Assistant"}


def invite_dialog(S: AppState, people: list[orgs.Person]) -> None:
    email = C.text_field("Email", keyboard_type=ft.KeyboardType.EMAIL)
    roles = ["member"] if S.role == "member" else (["member", "leader"] if S.role == "leader" else ["member", "leader", "admin"])
    if S.role == "member" and not S.profile.settings.get("members_can_recruit", True):
        roles = []
    if S.profile.settings.get("allow_assistants", True):
        roles.append("assistant")
    role = ft.Dropdown(label="Role", value=roles[0], border_radius=10,
                       options=[ft.DropdownOption(key=r, text="Assistant (works for me)" if r == "assistant"
                                                  else ROLE_LABEL[r]) for r in roles])
    branch = [p for p in people if S.ctx.is_admin or S.ctx.is_ancestor_of(p.path)]
    parent = ft.Dropdown(label="Joins the team of", value=str(S.ctx.membership_id), border_radius=10,
                         options=[ft.DropdownOption(key=str(p.membership_id),
                                                    text=("— " * p.depth) + p.name + (" (you)" if p.user_id == S.user.id else ""))
                                  for p in branch], visible=len(branch) > 1)

    async def submit():
        raw = await S.run(orgs.invite, S.ctx, email.value, role.value, uuid.UUID(parent.value))
        if not S.settings.smtp_host and not S.settings.is_production:
            link = f"{S.settings.base_url}/invite?token={raw}"
            S.page.show_dialog(ft.AlertDialog(title=ft.Text("Invitation created"), content=ft.Column([
                ft.Text("Email isn't configured on this server, so share this link yourself:"),
                ft.Text(link, selectable=True, size=12)], tight=True),
                actions=[ft.TextButton("Done", on_click=lambda e: S.page.pop_dialog())]))
        else:
            S.toast(f"Invitation sent to {email.value}.")
        await S.refresh()

    C.form_dialog(S.page, "Invite someone", [email, role, parent,
                                              ft.Text("They'll get an email with a link that expires in a week.",
                                                      size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
                  action(submit), "Send invitation")


def manage_menu(S: AppState, p: orgs.Person, people: list[orgs.Person]) -> ft.Control | None:
    if p.user_id == S.user.id or p.role == "owner" and S.role != "owner":
        return None
    if not (S.ctx.is_admin or (S.role == "leader" and S.ctx.is_ancestor_of(p.path))):
        return None

    def change_role(e):
        allowed = list(ROLES) if S.role == "owner" else (["member", "leader", "admin"] if S.ctx.is_admin else ["member", "leader"])
        dd = ft.Dropdown(label="Role", value=p.role, border_radius=10,
                         options=[ft.DropdownOption(key=r, text=ROLE_LABEL[r]) for r in allowed if r != "owner"])
        title = C.text_field("Title (optional)", value=p.title or "", hint_text="e.g. Senior Agent, Field Trainer")

        async def submit():
            await S.run(orgs.set_role, S.ctx, p.membership_id, dd.value, title.value)
            await S.refresh()

        C.form_dialog(S.page, f"Role for {p.name}", [dd, title], action(submit))

    def move(e):
        options = [x for x in people if x.membership_id != p.membership_id and not x.path.startswith(p.path + ".")
                   and (S.ctx.is_admin or S.ctx.is_ancestor_of(x.path))]
        dd = ft.Dropdown(label="New leader", border_radius=10, enable_filter=True,
                         options=([ft.DropdownOption(key="top", text="Top level (no leader)")] if S.ctx.is_admin else [])
                         + [ft.DropdownOption(key=str(x.membership_id), text=x.name) for x in options])

        async def submit():
            if not dd.value:
                S.toast("Choose a leader.", error=True)
                return False
            await S.run(orgs.move_member, S.ctx, p.membership_id, None if dd.value == "top" else uuid.UUID(dd.value))
            S.toast(f"Moved {p.name} and their team.")
            await S.refresh()

        C.form_dialog(S.page, f"Move {p.name}", [ft.Text("Their whole downline moves with them."), dd], action(submit), "Move")

    def release(e):
        note = C.text_field("Reason (kept in the audit log)")

        async def submit():
            await S.run(orgs.release_member, S.ctx, p.membership_id, note.value)
            S.toast(f"{p.name} was released. Their team now reports to their leader.")
            await S.refresh()

        C.form_dialog(S.page, f"Release {p.name}?", [
            ft.Text("They lose access immediately. Their downline moves up to their leader. "
                    "Their training history stays with the organization. Their private prospects are deleted "
                    "after 30 days — ask them to export their prospects first (Prospects → ⋮ → Export)."),
            note], action(submit), "Release")

    items = [ft.PopupMenuItem(content=ft.Text("Change role"), icon=ft.Icons.BADGE_OUTLINED, on_click=change_role),
             ft.PopupMenuItem(content=ft.Text("Move to another leader"), icon=ft.Icons.MOVE_UP, on_click=move)]
    if S.ctx.is_admin:
        items.append(ft.PopupMenuItem(content=ft.Text("Release from organization"), icon=ft.Icons.PERSON_REMOVE_OUTLINED,
                                      on_click=release))
    return ft.PopupMenuButton(items=items, icon=ft.Icons.MORE_VERT, tooltip="Manage")


async def people_view(S: AppState, q: dict) -> ft.Control:
    people, invites = await asyncio.gather(S.run(orgs.list_people, S.ctx, True), S.run(orgs.list_invitations, S.ctx))
    can_invite = not S.ctx.is_assistant and (S.role != "member" or S.profile.settings.get("members_can_recruit", True)
                                              or S.profile.settings.get("allow_assistants", True))
    me = next((p for p in people if p.user_id == S.user.id), None)
    upline = [p for p in people if me and me.path.startswith(p.path + ".")]
    team = [p for p in people if me and (p.path.startswith(me.path + ".") or (S.ctx.is_admin and p is not me
                                                                              and p not in upline))]
    blocks: list[ft.Control] = []
    if upline:
        blocks.append(C.section("Your leaders", *[ft.ListTile(
            leading=C.avatar(p.name), title=ft.Text(p.name), subtitle=ft.Text(p.title or ROLE_LABEL.get(p.role, p.role)),
            dense=True) for p in reversed(upline)]))
    base_depth = me.depth if me and not S.ctx.is_admin else 0
    rows = []
    for p in team:
        rows.append(ft.ListTile(
            leading=ft.Row([ft.Container(width=18 * max(0, p.depth - base_depth - (0 if S.ctx.is_admin else 1))),
                            C.avatar(p.name, 16)], tight=True, spacing=0),
            title=ft.Text(p.name, weight=ft.FontWeight.W_500),
            subtitle=ft.Text(" · ".join(x for x in [p.title or ROLE_LABEL.get(p.role, p.role), p.team_name, p.email] if x)),
            trailing=manage_menu(S, p, people),
            on_click=lambda e, u=p.user_id: S.go(f"/people/{u}")))
    title = "Everyone" if S.ctx.is_admin else "Your team"
    blocks.append(C.card(C.section(f"{title} ({len(team)})", *(rows or [ft.Text(
        "No one yet. Invite your first team member." if can_invite else "No one in your team yet.")]))))

    if invites:
        @action
        async def revoke(e):
            await S.run(orgs.revoke_invitation, S.ctx, e.control.data)
            await S.refresh()

        blocks.append(C.card(C.section("Pending invitations", *[ft.ListTile(
            leading=ft.Icon(ft.Icons.MAIL_OUTLINE), title=ft.Text(i.email),
            subtitle=ft.Text(f"{ROLE_LABEL.get(i.role, i.role)} · expires {C.when(i.expires_at)}"),
            trailing=ft.TextButton("Revoke", data=i.id, on_click=revoke)) for i in invites])))

    if S.role in ("leader", "admin", "owner") and S.profile.settings.get("allow_team_branding", True):
        team_name = C.text_field("Team name", value=S.profile.team_name if me and me.team_name else "",
                                 hint_text="e.g. Summit Producers")

        @action
        async def save_brand(e):
            await S.run(orgs.set_team_branding, S.ctx, team_name.value)
            await S.load_branding()
            S.toast("Team name saved.")
            await S.refresh()

        @action
        async def upload_logo(e):
            picked = await S.pick_and_upload([x.strip(".") for x in LOGO_EXT], "Choose a team logo")
            if picked:
                await S.run(orgs.set_team_branding, S.ctx, team_name.value, picked[0], picked[1])
                S.toast("Logo uploaded. It appears once it's processed.")

        blocks.append(C.card(C.section("Your team's brand", ft.Text(
            "Shown to everyone in your downline alongside the organization's brand.", size=12,
            color=ft.Colors.ON_SURFACE_VARIANT), team_name,
            ft.Row([ft.FilledTonalButton("Save", on_click=save_brand),
                    ft.OutlinedButton("Upload team logo", icon=ft.Icons.UPLOAD, on_click=upload_logo)], wrap=True))))

    actions = [ft.FilledButton("Invite", icon=ft.Icons.PERSON_ADD, on_click=lambda e: invite_dialog(S, people))] \
        if can_invite else []
    if q.get("invite") and can_invite:
        S.page.run_task(action(lambda: _later(lambda: invite_dialog(S, people))))
    return C.page_body(*blocks, title="People", actions=actions)


async def _later(fn):
    await asyncio.sleep(0.2)
    fn()
