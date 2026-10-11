"""Weekly updates: share progress with leaders and get feedback."""

from __future__ import annotations

import uuid

import flet as ft

from ...services import updates
from .. import components as C
from ..state import AppState, action

KIND_LABEL = {"comment": ("Comment", ft.Icons.CHAT_BUBBLE_OUTLINE), "coaching": ("Coaching", ft.Icons.SCHOOL_OUTLINED),
              "kudos": ("Kudos", ft.Icons.CELEBRATION_OUTLINED)}


def snapshot_view(snap: dict) -> ft.Control:
    rows: list[ft.Control] = []
    acts = snap.get("activity") or []
    if acts:
        rows.append(ft.Text("Activity this week", theme_style=ft.TextThemeStyle.LABEL_LARGE))
        grid = []
        for a in acts:
            target = a.get("target") or 0
            grid.append(ft.Container(ft.Column([
                ft.Text(a["label"], size=12, color=ft.Colors.ON_SURFACE_VARIANT, max_lines=1),
                ft.Text(f"{a['total']}" + (f" / {target}" if target else ""), weight=ft.FontWeight.W_700, size=18),
                ft.ProgressBar(value=min(1, a["total"] / target) if target else 0, bar_height=4, visible=bool(target)),
            ], spacing=2), col={"xs": 6, "sm": 4, "md": 3}, padding=8, border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH))
        rows.append(ft.ResponsiveRow(grid, spacing=8, run_spacing=8))
    if snap.get("goals"):
        rows.append(ft.Text("Goals", theme_style=ft.TextThemeStyle.LABEL_LARGE))
        rows += [C.progress(g["progress"] / 100, f"{g['title']} ({g['status']})") for g in snap["goals"]]
    if snap.get("paths"):
        rows.append(ft.Text("Onboarding", theme_style=ft.TextThemeStyle.LABEL_LARGE))
        rows += [C.progress(p["percent"] / 100, f"{p['title']} — {p['done']}/{p['total']} steps") for p in snap["paths"]]
    if snap.get("opportunities_shared"):
        rows.append(ft.Text(f"Opportunities shared with teammates: {snap['opportunities_shared']}"))
    return ft.Column(rows, spacing=8)


def update_row(S: AppState, u: updates.UpdateView, show_name: bool) -> ft.Control:
    who = f"{u.user_name} · " if show_name else ""
    fb = (f"{u.feedback_count} feedback" if u.feedback_count else "Awaiting feedback")
    return C.card(ft.ListTile(
        leading=C.avatar(u.user_name) if show_name else ft.Icon(ft.Icons.EVENT_NOTE_OUTLINED),
        title=ft.Text(f"{who}Week of {u.period_start.strftime('%b %d')}", weight=ft.FontWeight.W_600),
        subtitle=ft.Text((u.wins or u.help_wanted or u.blockers or "")[:140], max_lines=2),
        trailing=C.pill(fb, bgcolor=ft.Colors.TERTIARY_CONTAINER if not u.feedback_count else ft.Colors.SECONDARY_CONTAINER,
                        color=ft.Colors.ON_TERTIARY_CONTAINER if not u.feedback_count else ft.Colors.ON_SECONDARY_CONTAINER),
        on_click=lambda e: S.go(f"/updates/{u.id}")), padding=4)


async def updates_view(S: AppState, q: dict) -> ft.Control:
    tab = q.get("tab", "mine")
    if tab == "team" and not S.has_team():
        tab = "mine"
    tabs = ft.SegmentedButton(selected=[tab], segments=[
        ft.Segment(value="mine", label=ft.Text("My updates"), icon=ft.Icon(ft.Icons.PERSON_OUTLINE)),
        *([ft.Segment(value="team", label=ft.Text("Team updates"), icon=ft.Icon(ft.Icons.GROUPS_OUTLINED))]
          if S.has_team() else [])],
        on_change=lambda e: S.go(f"/updates?tab={e.control.selected[0]}"))
    blocks: list[ft.Control] = [tabs]

    if tab == "mine":
        snap = await S.run(updates.draft_snapshot, S.ctx)
        recips = await S.run(updates.possible_recipients, S.ctx)
        mine = await S.run(updates.my_updates, S.ctx)
        wins = C.text_field("Wins this week", multiline=True, min_lines=2, max_lines=6)
        blockers = C.text_field("What got in the way", multiline=True, min_lines=1, max_lines=6)
        help_ = C.text_field("Where I'd like help or feedback", multiline=True, min_lines=1, max_lines=6)
        boxes = [ft.Checkbox(label=name + (" (your leader)" if default else ""), value=default, data=uid)
                 for uid, name, default in recips]

        @action
        async def send(e):
            chosen = [b.data for b in boxes if b.value]
            await S.run(updates.send_update, S.ctx, wins=wins.value, blockers=blockers.value, help_wanted=help_.value,
                        recipient_ids=chosen)
            S.toast("Update sent.")
            await S.refresh()

        if recips:
            blocks.append(C.card(ft.Column([
                ft.Text("This week's update", theme_style=ft.TextThemeStyle.TITLE_MEDIUM, weight=ft.FontWeight.W_600),
                ft.Text("Your numbers are filled in automatically. Add a few lines and send it.",
                        color=ft.Colors.ON_SURFACE_VARIANT),
                snapshot_view(snap), ft.Divider(), wins, blockers, help_,
                ft.Text("Send to", theme_style=ft.TextThemeStyle.LABEL_LARGE), ft.Column(boxes, spacing=0),
                ft.Row([ft.FilledButton("Send update", icon=ft.Icons.SEND, on_click=send)]),
            ], spacing=10)))
        else:
            blocks.append(C.card(ft.Text("You're at the top of your organization, so there's no one above you to send "
                                         "updates to. Your team's updates appear under Team updates.")))
        if mine:
            blocks.append(C.section("Sent", *[update_row(S, u, False) for u in mine]))
    else:
        awaiting = q.get("awaiting", "1") == "1"
        items = await S.run(updates.leader_inbox, S.ctx, awaiting)
        blocks.append(ft.Switch(label="Only show updates waiting for my feedback", value=awaiting,
                                on_change=lambda e: S.go(f"/updates?tab=team&awaiting={'1' if e.control.value else '0'}")))
        blocks += [update_row(S, u, True) for u in items] or [C.empty(
            "No updates waiting for feedback." if awaiting else "No updates from your team yet.")]
    async def live(event):
        if S.page.route.startswith("/updates") and "/updates/" not in S.page.route:
            await S.refresh()

    S.live_handlers["updates"] = live
    return C.page_body(*blocks, title="Updates", subtitle="Share progress and get feedback every week")


async def update_detail_view(S: AppState, q: dict, update_id: str) -> ft.Control:
    u = await S.run(updates.get_update, S.ctx, uuid.UUID(update_id))
    blocks: list[ft.Control] = [ft.TextButton("Back", icon=ft.Icons.ARROW_BACK,
                                              on_click=lambda e: S.go("/updates" if u.is_mine else "/updates?tab=team"))]
    texts = []
    for label, val in (("Wins", u.wins), ("What got in the way", u.blockers), ("Help wanted", u.help_wanted)):
        if val:
            texts += [ft.Text(label, theme_style=ft.TextThemeStyle.LABEL_LARGE), ft.Text(val, selectable=True)]
    blocks.append(C.card(ft.Column([*texts, ft.Divider(), snapshot_view(u.snapshot)], spacing=8)))

    fb_rows = []
    for f in u.feedback:
        label, icon = KIND_LABEL.get(f.kind, ("Comment", ft.Icons.CHAT_BUBBLE_OUTLINE))
        fb_rows.append(ft.ListTile(leading=C.avatar(f.author_name), title=ft.Row([
            ft.Text(f.author_name, weight=ft.FontWeight.W_600), C.pill(label, icon=icon),
            ft.Text(C.when(f.created_at), size=12, color=ft.Colors.ON_SURFACE_VARIANT)], wrap=True),
            subtitle=ft.Text(f.body, selectable=True)))
    body = C.text_field("Reply" if u.is_mine else "Your feedback", multiline=True, min_lines=2, max_lines=8)
    kind = ft.SegmentedButton(selected=["comment"], segments=[
        ft.Segment(value=k, label=ft.Text(v[0]), icon=ft.Icon(v[1])) for k, v in KIND_LABEL.items()],
        visible=not u.is_mine)

    @action
    async def send(e):
        await S.run(updates.add_feedback, S.ctx, u.id, body.value, kind.selected[0] if not u.is_mine else "comment")
        S.toast("Reply sent." if u.is_mine else f"Feedback sent to {u.user_name}. They've been notified.")
        await S.refresh()

    async def live(event):
        if event.get("update_id") == update_id and S.page.route == f"/updates/{update_id}":
            await S.refresh()

    S.live_handlers["updates"] = live

    blocks.append(C.card(C.section("Feedback", *(fb_rows or [ft.Text("No feedback yet.", color=ft.Colors.ON_SURFACE_VARIANT)]),
                                   kind, body, ft.Row([ft.FilledButton("Send", icon=ft.Icons.SEND, on_click=send)]))))
    return C.page_body(*blocks, title=f"{'Your' if u.is_mine else u.user_name + chr(39) + 's'} week of "
                                      f"{u.period_start.strftime('%B %d')}", subtitle=f"Sent {C.when(u.created_at)}")
