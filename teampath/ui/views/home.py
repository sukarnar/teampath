"""Today: daily activity, next steps, focus people and what needs a leader's attention."""

from __future__ import annotations

import asyncio
from datetime import datetime

import flet as ft

from ...services import activity, goals, paths, qa, teammap, updates
from ...services import prospects as prospects_service
from ...services import tasks as tasks_service
from .. import components as C
from ..state import AppState, action


def greeting(name: str) -> str:
    h = datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    return f"Good {part}, {name.split()[0]}"


def activity_tile(S: AppState, t: activity.ActivityTile) -> ft.Control:
    count = ft.Text(str(t.today), size=30, weight=ft.FontWeight.W_700)
    week = ft.Text(f"{t.week} this week", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    bar = ft.ProgressBar(value=min(1.0, t.today / t.daily_target) if t.daily_target else 0, bar_height=6,
                         border_radius=6, visible=t.daily_target > 0)
    target = ft.Text(f"Goal {t.daily_target}/day" if t.daily_target else "", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    met = ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.PRIMARY, size=18, visible=t.target_met, tooltip="Daily goal met")

    async def change(delta: int):
        await S.run(activity.log, S.ctx, t.type_id, delta)
        t.today = max(0, t.today + delta)
        t.week = max(0, t.week + delta)
        count.value = str(t.today)
        week.value = f"{t.week} this week"
        bar.value = min(1.0, t.today / t.daily_target) if t.daily_target else 0
        met.visible = t.target_met

    return ft.Container(
        C.card(ft.Column([
            ft.Row([ft.Text(t.label, weight=ft.FontWeight.W_600, expand=True, max_lines=2), met]),
            ft.Row([count, ft.Container(expand=True),
                    ft.IconButton(ft.Icons.REMOVE, tooltip=f"Undo one {t.label.lower()}",
                                  on_click=action(lambda e: change(-1)), disabled=False),
                    ft.FilledTonalButton("+1", on_click=action(lambda e: change(1)), tooltip=f"Log one {t.label.lower()}")]),
            bar, ft.Row([target, ft.Container(expand=True), week]),
        ], spacing=6), padding=14),
        col={"xs": 12, "sm": 6, "md": 4, "lg": 3},
    )


def person_row(S: AppState, item: teammap.FocusItem, pinned: bool) -> ft.Control:
    n = item.node

    @action
    async def unpin(e):
        await S.run(teammap.toggle_pin, S.ctx, n.user_id)
        await S.refresh()

    return ft.ListTile(
        leading=C.avatar(n.name), title=ft.Text(n.name, weight=ft.FontWeight.W_600),
        subtitle=ft.Text(item.suggestion), on_click=lambda e: S.go(f"/people/{n.user_id}"),
        trailing=ft.Row([C.status_pill(n.momentum),
                         *([ft.IconButton(ft.Icons.PUSH_PIN, tooltip="Unpin", on_click=unpin)] if pinned else [])],
                        tight=True, spacing=4),
    )


def tasks_card(S: AppState, summary, rows) -> ft.Control | None:
    from .tasks import task_row

    if not rows and not summary.delegated_open and not S.ctx.is_assistant:
        return None
    parts = []
    if summary.overdue:
        parts.append(f"{summary.overdue} overdue")
    if summary.due_today:
        parts.append(f"{summary.due_today} due today")
    if summary.delegated_open:
        parts.append(f"{summary.delegated_open} delegated open"
                     + (f" ({summary.delegated_overdue} overdue)" if summary.delegated_overdue else ""))
    if summary.delegated_done_today:
        parts.append(f"{summary.delegated_done_today} done by your assistants today")
    return C.card(C.section(
        "Tasks", *([ft.Text(" · ".join(parts), color=ft.Colors.ERROR if summary.overdue
                                                 else ft.Colors.ON_SURFACE_VARIANT)] if parts else []),
        *[task_row(S, r) for r in rows[:6]],
        *([ft.Text("Nothing assigned to you right now.", color=ft.Colors.ON_SURFACE_VARIANT)] if not rows else []),
        trailing=ft.TextButton("All tasks", on_click=lambda e: S.go("/tasks"))))


async def home_view(S: AppState, q: dict) -> ft.Control:
    ctx = S.ctx
    tiles, enrollments = await asyncio.gather(S.run(activity.tiles, ctx), S.run(paths.my_enrollments, ctx))
    blocks: list[ft.Control] = []
    task_summary, my_tasks = await asyncio.gather(S.run(tasks_service.summary, ctx),
                                                  S.run(tasks_service.assigned_to_me, ctx))
    today = ctx.today()
    due_soon = [r for r in my_tasks if r.due_on and r.due_on <= today] + \
               [r for r in my_tasks if not r.due_on or r.due_on > today]

    if S.profile.welcome_text and all(e.done == 0 for e in enrollments) and enrollments:
        blocks.append(ft.Container(C.split(S.profile.welcome_text), padding=16, border_radius=12,
                                   bgcolor=ft.Colors.PRIMARY_CONTAINER))

    if S.has_team():
        focus, attention, pending, inbox, awaiting, goal_queue = await asyncio.gather(
            S.run(teammap.my_focus, ctx), S.run(teammap.attention_list, ctx, 5), S.run(paths.pending_signoffs, ctx),
            S.run(qa.inbox, ctx), S.run(updates.leader_inbox, ctx, True), S.run(goals.pending_approvals, ctx))

        def tile(n, label, icon, route):
            return ft.Container(C.stat(str(n), label, icon), on_click=lambda e: S.go(route), border_radius=12,
                                col={"xs": 6, "md": 3})

        blocks.append(ft.ResponsiveRow([
            tile(len(goal_queue), "Goals to approve", ft.Icons.FACT_CHECK_OUTLINED, "/goals?tab=approve"),
            tile(len(pending), "Sign-offs waiting", ft.Icons.TASK_ALT, "/team-progress"),
            tile(len(inbox), "Questions to answer", ft.Icons.HELP_OUTLINE, "/questions"),
            tile(len(awaiting), "Updates awaiting feedback", ft.Icons.FORUM_OUTLINED, "/updates?tab=team"),
        ], spacing=12, run_spacing=12))
        focus_list = [person_row(S, f, True) for f in focus] or [ft.Text(
            "Pin up to five people from your team map to keep them in front of you every day.",
            color=ft.Colors.ON_SURFACE_VARIANT)]
        blocks.append(C.card(C.section("Today's focus", *focus_list,
                                       trailing=ft.TextButton("Open team map", icon=ft.Icons.BUBBLE_CHART_OUTLINED,
                                                              on_click=lambda e: S.go("/team")))))
        if attention:
            blocks.append(C.card(C.section("Could use your attention", *[person_row(S, f, False) for f in attention])))

    fresh = await S.run(updates.unseen_feedback, ctx)
    if fresh:
        kind_label = {"kudos": "Kudos", "coaching": "Coaching", "comment": "Feedback"}
        rows = [ft.ListTile(
            leading=C.avatar(f.author_name),
            title=ft.Row([ft.Text(f.author_name, weight=ft.FontWeight.W_600), C.pill(kind_label.get(f.kind, "Feedback")),
                          ft.Text(f"on week of {f.period_start:%b %d}", size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
                         wrap=True, spacing=6),
            subtitle=ft.Text(f.body, max_lines=3), on_click=lambda e, i=f.update_id: S.go(f"/updates/{i}"))
            for f in fresh]
        blocks.insert(0, C.card(C.section("New feedback for you", *rows), bgcolor=ft.Colors.SECONDARY_CONTAINER))

    card = tasks_card(S, task_summary, due_soon)
    if card:
        blocks.append(card)

    due_today, overdue = await S.run(prospects_service.due_counts, ctx)
    if due_today or overdue:
        async def start_followups():
            lid = await S.run(prospects_service.focus_from, ctx, "due")
            S.go(f"/prospects/focus/{lid}")

        parts = ([f"{overdue} overdue"] if overdue else []) + ([f"{due_today} due today"] if due_today else [])
        blocks.append(C.banner(ft.Icons.CONTACT_PHONE_OUTLINED, "Prospect follow-ups", " · ".join(parts), [
            ft.TextButton("View", on_click=lambda e: S.go("/prospects")),
            ft.FilledButton("Start follow-ups", icon=ft.Icons.PLAY_ARROW, on_click=action(lambda e: start_followups())),
        ], subtitle_color=ft.Colors.ERROR if overdue else None))

    if not ctx.is_assistant:
        blocks.append(C.section("Today's activity", ft.ResponsiveRow([activity_tile(S, t) for t in tiles], spacing=12,
                                                                      run_spacing=12),
                                trailing=ft.Text(today.strftime("%A, %B %d"), color=ft.Colors.ON_SURFACE_VARIANT)))

    next_steps = []
    for e in enrollments:
        if e.status != "active":
            continue
        for s in e.steps:
            if s.status == "todo" and not s.locked:
                next_steps.append((e, s))
                break
    step_rows = [ft.ListTile(
        leading=ft.Icon(ft.Icons.ERROR_OUTLINE if s.overdue else ft.Icons.RADIO_BUTTON_UNCHECKED,
                        color=ft.Colors.ERROR if s.overdue else ft.Colors.PRIMARY),
        title=ft.Text(s.title, weight=ft.FontWeight.W_500),
        subtitle=ft.Text(f"{e.title} · {'overdue since' if s.overdue else 'due'} {C.when(s.due_on)}"),
        trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT), on_click=lambda ev, sid=s.id: S.go(f"/learn/step/{sid}"))
        for e, s in next_steps]
    if enrollments:
        blocks.append(C.card(C.section("Next steps", *(step_rows or [ft.Text("You're all caught up on your onboarding.")]),
                                       trailing=ft.TextButton("All paths", on_click=lambda e: S.go("/learn")))))

    if today.weekday() >= 3 and not ctx.is_assistant:
        mine = await S.run(updates.my_updates, ctx)
        start, _ = updates.current_period()
        if not any(u.period_start == start for u in mine):
            blocks.append(ft.Container(ft.Row([
                ft.Icon(ft.Icons.EDIT_NOTE, color=ft.Colors.ON_SECONDARY_CONTAINER),
                ft.Text("Share this week's progress with your leader.", expand=True,
                        color=ft.Colors.ON_SECONDARY_CONTAINER),
                ft.FilledButton("Write update", on_click=lambda e: S.go("/updates")),
            ]), padding=14, border_radius=12, bgcolor=ft.Colors.SECONDARY_CONTAINER))

    async def live(event):
        if event.get("kind") in ("activity", "progress", "qa", "goals", "updates", "tasks") and S.page.route == "/":
            await S.refresh()

    S.live_handlers["progress"] = S.live_handlers["qa"] = S.live_handlers["goals"] = live
    S.live_handlers["updates"] = S.live_handlers["tasks"] = live
    return C.page_body(*blocks, title=greeting(S.user.name))
