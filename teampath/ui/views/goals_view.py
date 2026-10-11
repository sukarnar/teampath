"""Goals: propose, track progress, and (leaders) approve."""

from __future__ import annotations

import uuid
from datetime import date

import flet as ft

from ...services import goals
from .. import components as C
from ..state import AppState, action

STATUS = {"proposed": ("Waiting for approval", ft.Icons.HOURGLASS_EMPTY), "active": ("Active", ft.Icons.FLAG),
          "done": ("Done", ft.Icons.EMOJI_EVENTS_OUTLINED), "dropped": ("Dropped", ft.Icons.DO_NOT_DISTURB_ON_OUTLINED)}


def goal_card(S: AppState, g, can_manage: bool, mine: bool) -> ft.Control:
    label, icon = STATUS.get(g.status, (g.status, ft.Icons.FLAG))
    pct = ft.Text(f"{g.progress}%", weight=ft.FontWeight.W_600, width=44, text_align=ft.TextAlign.RIGHT)

    @action
    async def save_progress(e):
        await S.run(goals.update_goal, S.ctx, g.id, progress=int(e.control.value))
        if int(e.control.value) == 100 and g.status == "active":
            S.toast("Goal completed — congratulations!")
            await S.refresh()

    def on_slide(e):
        pct.value = f"{int(e.control.value)}%"

    @action
    async def set_status(e):
        await S.run(goals.update_goal, S.ctx, g.id, status=e.control.data)
        await S.refresh()

    buttons = []
    if g.status == "proposed" and can_manage and not mine:
        buttons.append(ft.FilledTonalButton("Approve", icon=ft.Icons.CHECK, data="active", on_click=set_status))
    if g.status in ("active", "proposed") and (mine or can_manage):
        buttons.append(ft.TextButton("Drop", data="dropped", on_click=set_status))
    if g.status == "dropped" and (mine or can_manage):
        buttons.append(ft.TextButton("Re-open", data="active" if can_manage and not mine else "proposed",
                                     on_click=set_status))
    slider = ft.Slider(value=g.progress, min=0, max=100, divisions=20, label="{value}%", expand=True,
                       on_change=on_slide, on_change_end=save_progress, disabled=not mine or g.status != "active")
    return C.card(ft.Column([
        ft.Row([ft.Icon(icon, color=ft.Colors.PRIMARY), ft.Text(g.title, weight=ft.FontWeight.W_600, expand=True),
                C.pill(label)]),
        *([ft.Text(g.detail, color=ft.Colors.ON_SURFACE_VARIANT)] if g.detail else []),
        ft.Row([slider, pct]),
        ft.Row([ft.Text(f"Target {C.when(g.target_date)}" if g.target_date else "", size=12,
                        color=ft.Colors.ERROR if g.target_date and g.target_date < date.today() and g.status == "active"
                        else ft.Colors.ON_SURFACE_VARIANT, expand=True), *buttons]),
    ], spacing=6))


def new_goal_dialog(S: AppState, for_user_id: uuid.UUID | None = None, for_name: str | None = None) -> None:
    title = C.text_field("Goal", hint_text="e.g. Get appointed with 3 carriers")
    detail = C.text_field("How you'll measure it (optional)", multiline=True, min_lines=2, max_lines=4)
    horizon = ft.SegmentedButton(selected=["30"], segments=[ft.Segment(value=v, label=ft.Text(f"{v} days"))
                                                            for v in ("30", "60", "90")])

    async def submit():
        await S.run(goals.create_goal, S.ctx, title.value, detail.value, horizon.selected[0], None, for_user_id)
        S.toast("Goal set." if for_user_id else "Goal proposed — your leader will review it.")
        await S.refresh()

    C.form_dialog(S.page, f"New goal for {for_name}" if for_name else "Propose a goal",
                  [title, detail, ft.Text("Timeframe", size=12), horizon], action(submit),
                  "Set goal" if for_user_id else "Propose")


def approval_card(S: AppState, g, name: str) -> ft.Control:
    @action
    async def approve(e):
        await S.run(goals.update_goal, S.ctx, g.id, status="active")
        S.toast(f"Approved {name}'s goal.")
        await S.refresh()

    def send_back(e):
        note = C.text_field("What should they change?", multiline=True, min_lines=2, max_lines=6,
                            hint_text="e.g. Make it measurable — how many carriers, by when?")

        async def submit():
            await S.run(goals.decline_goal, S.ctx, g.id, note.value)
            S.toast(f"Sent back to {name}.")
            await S.refresh()

        C.form_dialog(S.page, f"Send back to {name}", [ft.Text(g.title, weight=ft.FontWeight.W_600), note],
                      action(submit), "Send back")

    return C.card(ft.Column([
        ft.Row([C.avatar(name), ft.Column([ft.Text(name, weight=ft.FontWeight.W_600),
                                           ft.Text(f"Proposed {C.when(g.created_at)}", size=12,
                                                   color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0, expand=True),
                C.pill(f"{g.horizon} days" if g.horizon != "custom" else "Custom date")]),
        ft.Text(g.title, theme_style=ft.TextThemeStyle.TITLE_MEDIUM, weight=ft.FontWeight.W_600),
        *([ft.Text(g.detail, color=ft.Colors.ON_SURFACE_VARIANT)] if g.detail else []),
        *([ft.Text(f"Target {g.target_date:%b %d, %Y}", size=12)] if g.target_date else []),
        ft.Row([ft.FilledButton("Approve", icon=ft.Icons.CHECK, on_click=approve),
                ft.OutlinedButton("Send back", icon=ft.Icons.UNDO, on_click=send_back),
                ft.TextButton("Profile", on_click=lambda e: S.go(f"/people/{g.user_id}"))], wrap=True),
    ], spacing=8))


async def goals_view(S: AppState, q: dict) -> ft.Control:
    pending = await S.run(goals.pending_approvals, S.ctx) if S.has_team() else []
    tab = q.get("tab", "approve" if pending else "mine")
    if not S.has_team():
        tab = "mine"
    blocks: list[ft.Control] = []
    if S.has_team():
        blocks.append(ft.SegmentedButton(selected=[tab], segments=[
            ft.Segment(value="mine", label=ft.Text("My goals"), icon=ft.Icon(ft.Icons.PERSON_OUTLINE)),
            ft.Segment(value="approve", label=ft.Text(f"To approve ({len(pending)})" if pending else "To approve"),
                       icon=ft.Icon(ft.Icons.FACT_CHECK_OUTLINED))],
            on_change=lambda e: S.go(f"/goals?tab={e.control.selected[0]}")))

    async def live(event):
        if S.page.route.startswith("/goals"):
            await S.refresh()

    S.live_handlers["goals"] = live

    if tab == "approve":
        blocks += [approval_card(S, g, name) for g, name in pending] or [
            C.empty("No goals waiting for your approval.", ft.Icons.FACT_CHECK_OUTLINED)]
        return C.page_body(*blocks, title="Goals", subtitle="Goals your team proposed, waiting for you")

    items = await S.run(goals.list_goals, S.ctx)
    active = [g for g in items if g.status in ("active", "proposed")]
    closed = [g for g in items if g.status in ("done", "dropped")]
    blocks += [goal_card(S, g, False, True) for g in active] or [
        C.empty("No goals yet. Propose your first 30-day goal.", ft.Icons.FLAG_OUTLINED)]
    if closed:
        blocks.append(C.section("Finished or sent back", *[goal_card(S, g, False, True) for g in closed]))
    return C.page_body(*blocks, title="Goals", subtitle="Agree them with your leader, then track progress",
                       actions=[ft.FilledButton("Propose a goal", icon=ft.Icons.ADD,
                                                on_click=lambda e: new_goal_dialog(S))])
