"""Platform administration: approve, suspend and reactivate organizations."""

from __future__ import annotations

import flet as ft

from ...services import platform
from .. import components as C
from ..state import AppState, action


async def platform_view(S: AppState, q: dict) -> ft.Control:
    if not S.user.is_platform_admin:
        S.go("/")
        return None
    status = q.get("status", "pending")
    rows = await S.run(platform.list_orgs, S.user.id, None if status == "all" else status)
    tabs = ft.SegmentedButton(selected=[status], segments=[ft.Segment(value=v, label=ft.Text(v.title()))
                                                           for v in ("pending", "active", "suspended", "all")],
                              on_change=lambda e: S.go(f"/platform?status={e.control.selected[0]}"))

    @action
    async def set_status(e):
        org_id, new = e.control.data
        await S.run(platform.set_org_status, S.user.id, org_id, new)
        S.toast(f"Organization {new}.")
        await S.refresh()

    cards = []
    for o in rows:
        btns = []
        if o.status in ("pending", "suspended"):
            btns.append(ft.FilledButton("Approve" if o.status == "pending" else "Reactivate", data=(o.id, "active"),
                                        on_click=set_status))
        if o.status in ("pending", "active"):
            btns.append(ft.TextButton("Suspend", data=(o.id, "suspended"), on_click=set_status))
        cards.append(C.card(ft.ListTile(
            leading=C.avatar(o.name), title=ft.Text(o.name, weight=ft.FontWeight.W_600),
            subtitle=ft.Text(f"{o.org_type} · {o.members} members · owner {o.owner_email} · created {C.when(o.created_at)}"),
            trailing=ft.Row(btns, tight=True)), padding=4))
    return C.page_body(tabs, *cards or [C.empty("Nothing here.")], title="Platform admin",
                       subtitle="Organizations on this installation")
