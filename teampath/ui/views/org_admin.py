"""Organization settings: details, branding, security & privacy, activity types, audit log."""

from __future__ import annotations

import flet as ft

from ...services import orgs
from ...storage import LOGO_EXT
from .. import components as C
from ..state import AppState, action

TABS = [("profile", "Details", ft.Icons.APARTMENT_OUTLINED), ("brand", "Branding", ft.Icons.PALETTE_OUTLINED),
        ("settings", "Security & privacy", ft.Icons.SHIELD_OUTLINED), ("activity", "Activity types", ft.Icons.CHECKLIST),
        ("audit", "Audit log", ft.Icons.HISTORY)]
SWATCHES = ["#3355CC", "#0F6E56", "#7A3FBF", "#B4371F", "#C0821A", "#1F6FA8", "#2F3B52", "#C2185B"]


async def org_view(S: AppState, q: dict) -> ft.Control:
    S.ctx.require_admin()
    tab = q.get("tab", "profile")
    p = await S.run(orgs.get_profile, S.ctx)
    tabs = ft.SegmentedButton(selected=[tab], show_selected_icon=False,
                              segments=[ft.Segment(value=k, label=ft.Text(l), icon=ft.Icon(i)) for k, l, i in TABS],
                              on_change=lambda e: S.go(f"/org?tab={e.control.selected[0]}"))
    blocks: list[ft.Control] = [ft.Row([tabs], scroll=ft.ScrollMode.AUTO)]

    if tab == "profile":
        f = {k: C.text_field(label, value=getattr(p, k) or "", **kw) for k, label, kw in [
            ("name", "Organization name", {}), ("website", "Website", {}),
            ("contact_email", "Contact email", {"keyboard_type": ft.KeyboardType.EMAIL}),
            ("phone", "Phone", {"keyboard_type": ft.KeyboardType.PHONE}),
            ("address", "Address", {"multiline": True, "min_lines": 2, "max_lines": 4}),
            ("timezone", "Time zone", {"hint_text": "e.g. America/New_York"})]}

        @action
        async def save(e):
            await S.run(orgs.update_profile, S.ctx, **{k: v.value for k, v in f.items()})
            await S.load_branding()
            S.toast("Saved.")
            await S.refresh()

        blocks.append(C.card(ft.Column([*f.values(), ft.Row([ft.FilledButton("Save", on_click=save)]),
                                        ft.Text(f"Type: {'Insurance agency' if p.org_type == 'insurance' else 'General'}"
                                                f" · address: {p.slug}", size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
                                       spacing=12)))
    elif tab == "brand":
        color = C.text_field("Brand color", value=p.primary_color, width=180)
        tagline = C.text_field("Tagline", value=p.tagline or "")
        welcome = C.text_field("Welcome message for new members (Markdown)", value=p.welcome_text or "",
                               multiline=True, min_lines=3, max_lines=10)
        preview = ft.Container(width=36, height=36, border_radius=18, bgcolor=p.primary_color)

        def pick(e):
            color.value = e.control.data
            preview.bgcolor = e.control.data

        swatches = ft.Row([ft.Container(width=28, height=28, border_radius=14, bgcolor=c, data=c, on_click=pick,
                                        tooltip=c) for c in SWATCHES], wrap=True)

        @action
        async def save(e):
            await S.run(orgs.update_branding, S.ctx, primary_color=color.value.strip(), tagline=tagline.value,
                        welcome_text=welcome.value)
            await S.load_branding()
            S.toast("Branding saved.")
            await S.refresh()

        @action
        async def upload(e):
            res = await S.pick_and_upload([x.strip(".") for x in LOGO_EXT], "Choose your logo (PNG or JPG)")
            if res:
                await S.run(orgs.set_logo, S.ctx, res[0], res[1])
                S.toast("Logo uploaded. It appears in a moment, once it's processed.")

        logo = (ft.Image(src=S.logo_url, height=64, fit=ft.BoxFit.CONTAIN) if S.logo_url
                else ft.Text("No logo yet", color=ft.Colors.ON_SURFACE_VARIANT))
        blocks.append(C.card(ft.Column([
            C.section("Logo", logo, ft.Row([ft.OutlinedButton("Upload logo", icon=ft.Icons.UPLOAD, on_click=upload)]),
                      ft.Text("PNG or JPG. It's re-encoded and resized on our side.", size=12,
                              color=ft.Colors.ON_SURFACE_VARIANT)),
            ft.Divider(),
            C.section("Color", ft.Row([preview, color]), swatches),
            ft.Divider(), tagline, welcome, ft.Row([ft.FilledButton("Save branding", on_click=save)]),
        ], spacing=12)))
    elif tab == "settings":
        s = p.settings
        vis = ft.RadioGroup(value=s.get("activity_visibility", "auto"), content=ft.Column([
            ft.Radio(value="auto", label="Leaders see their team's daily activity automatically"),
            ft.Radio(value="shared", label="Agents share activity only through their weekly updates")]))
        mfa = {r: ft.Checkbox(label=label, value=r in s.get("mfa_required_roles", []))
               for r, label in (("owner", "Owners"), ("admin", "Admins"), ("leader", "Leaders"), ("member", "Everyone"))}
        recruit = ft.Switch(label="Agents can invite recruits into their own team", value=s.get("members_can_recruit", True))
        team_brand = ft.Switch(label="Leaders can name and brand their team", value=s.get("allow_team_branding", True))
        expiry = ft.Dropdown(label="Invitations expire after", value=str(s.get("invite_expiry_days", 7)), width=240,
                             border_radius=10, options=[ft.DropdownOption(key=str(d), text=f"{d} days") for d in (2, 7, 14)])

        months = [(m, f"{m} months") for m in (3, 6, 12, 18, 24, 36, 60)]
        arch = ft.Dropdown(label="Archive prospects with no activity after", value=str(s.get("prospect_archive_months", 12)),
                           width=300, border_radius=10, options=[ft.DropdownOption(key=str(m), text=t) for m, t in months])
        dele = ft.Dropdown(label="Delete archived prospects after", value=str(s.get("prospect_delete_months", 24)),
                           width=300, border_radius=10,
                           options=[ft.DropdownOption(key=str(m), text=f"{t} without activity") for m, t in months])
        note_tags = C.text_field("Note topics (comma-separated)", value=", ".join(s.get("note_tags") or []))

        @action
        async def save(e):
            if int(dele.value) <= int(arch.value):
                S.toast("Deletion must come after archiving.", error=True)
                return
            await S.run(orgs.update_settings, S.ctx, prospect_archive_months=int(arch.value),
                        prospect_delete_months=int(dele.value))
            await S.run(orgs.update_settings, S.ctx, activity_visibility=vis.value,
                        mfa_required_roles=[r for r, cb in mfa.items() if cb.value], members_can_recruit=recruit.value,
                        allow_team_branding=team_brand.value, invite_expiry_days=int(expiry.value),
                        note_tags=[t.strip() for t in (note_tags.value or "").split(",") if t.strip()])
            S.toast("Settings saved. They apply the next time people sign in.")

        blocks.append(C.card(ft.Column([
            C.section("Daily activity privacy", vis),
            ft.Divider(), C.section("Require two-step verification for", ft.Row(list(mfa.values()), wrap=True)),
            ft.Divider(), C.section("Teams", recruit, team_brand, expiry),
            ft.Divider(), C.section("Prospects", ft.Text(
                "Prospects belong to each agent and are visible only to them. When someone is released, their "
                "prospects are deleted after 30 days — nobody else receives them.", size=12,
                color=ft.Colors.ON_SURFACE_VARIANT), ft.Row([arch, dele], wrap=True)),
            ft.Divider(), C.section("Notes", ft.Text(
                "Topics offered when people take notes at trainings and conferences. They can add their own. "
                "Notes tagged Financial concepts or Compliance show a “not compliance-reviewed” label when "
                "shared beyond the author's leader.", size=12, color=ft.Colors.ON_SURFACE_VARIANT), note_tags),
            ft.Row([ft.FilledButton("Save settings", on_click=save)]),
        ], spacing=14)))
    elif tab == "activity":
        types = await S.run(orgs.list_activity_types, S.ctx, True)
        rows = []
        for t in types:
            label = C.text_field("Label", value=t.label, expand=True, dense=True)
            target = C.text_field("Daily goal", value=str(t.daily_target), width=120, dense=True,
                                  keyboard_type=ft.KeyboardType.NUMBER)
            active = ft.Switch(value=t.active, tooltip="Shown to agents")

            async def save_one(e, t=t, label=label, target=target, active=active):
                await S.run(orgs.save_activity_type, S.ctx, label.value, int(target.value or 0), t.id, active.value)
                S.toast("Saved.")

            rows.append(ft.Row([label, target, active, ft.IconButton(ft.Icons.SAVE_OUTLINED, tooltip="Save",
                                                                     on_click=action(save_one))]))
        new_label = C.text_field("New activity", expand=True, dense=True, hint_text="e.g. Follow-up calls")
        new_target = C.text_field("Daily goal", value="0", width=120, dense=True, keyboard_type=ft.KeyboardType.NUMBER)

        @action
        async def add(e):
            await S.run(orgs.save_activity_type, S.ctx, new_label.value, int(new_target.value or 0))
            await S.refresh()

        blocks.append(C.card(ft.Column([ft.Text("What your people log each day, with an optional daily goal.",
                                                color=ft.Colors.ON_SURFACE_VARIANT), *rows, ft.Divider(),
                                        ft.Row([new_label, new_target, ft.FilledTonalButton("Add", on_click=add)])],
                                       spacing=10)))
    elif tab == "audit":
        entries = await S.run(orgs.audit_entries, S.ctx, 200)
        table = ft.DataTable(columns=[ft.DataColumn(ft.Text(h)) for h in ("When", "Who", "What", "Detail")], rows=[
            ft.DataRow(cells=[ft.DataCell(ft.Text(a.created_at.strftime("%b %d %H:%M"), size=12)),
                              ft.DataCell(ft.Text(name or "System", size=12)),
                              ft.DataCell(ft.Text(a.action, size=12)),
                              ft.DataCell(ft.Text(", ".join(f"{k}: {v}" for k, v in (a.detail or {}).items())[:80],
                                                  size=12))]) for a, name in entries])
        blocks.append(C.card(ft.Row([table], scroll=ft.ScrollMode.AUTO), padding=4))
    return C.page_body(*blocks, title="Organization", subtitle=p.name)
