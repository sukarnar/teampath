"""Team map (bubble chart), person profile, team progress and opportunity sharing."""

from __future__ import annotations

import asyncio
import uuid

import flet as ft

from ...services import activity, goals, orgs, paths, teammap, updates
from .. import components as C
from ..state import AppState, action
from ..theme import STATUS_COLORS, STATUS_ICONS
from .goals_view import goal_card, new_goal_dialog
from .learn import enrollment_card

LABELS = {"active": "Active", "slowing": "Slowing", "inactive": "Inactive", "new": "New"}


# ------------------------------------------------------------ bubbles

def _bubble(node: teammap.MapNode, d: float, enclosure: bool, selected: bool, on_click) -> ft.Control:
    fill, text_color = STATUS_COLORS.get(node.momentum, STATUS_COLORS["new"])
    tip = (f"{node.name} · {LABELS.get(node.momentum)} · {node.week_activity} activities this week"
           + (f" · team of {node.team_size}" if node.team_size else "")
           + (f" · onboarding {node.onboarding_percent}%" if node.onboarding_percent is not None else ""))
    border = ft.Border.all(3 if selected else (1.5 if enclosure else 0), ft.Colors.PRIMARY if selected else fill)
    if enclosure:
        label = ft.Container(
            ft.Text(node.team_name or node.name, size=12, weight=ft.FontWeight.W_600, max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.CENTER),
            alignment=ft.Alignment.TOP_CENTER, padding=ft.Padding.only(top=max(4, d * 0.04), left=d * 0.2, right=d * 0.2))
        circle = ft.Container(label, width=d, height=d, border_radius=d / 2, border=border,
                              bgcolor=ft.Colors.with_opacity(0.10, fill), on_click=on_click, tooltip=tip)
        return circle
    show_name = d >= 64
    inner = ft.Column([
        ft.Text(C.initials(node.name) if not show_name else node.name.split()[0], color=text_color,
                weight=ft.FontWeight.W_700, size=max(10, min(16, d / 5)), max_lines=1, text_align=ft.TextAlign.CENTER),
        *([ft.Icon(STATUS_ICONS.get(node.momentum), color=text_color, size=14)] if d >= 48 else []),
    ], spacing=0, alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True)
    layers: list[ft.Control] = [ft.Container(inner, width=d, height=d, border_radius=d / 2, bgcolor=fill, border=border,
                                             alignment=ft.Alignment.CENTER)]
    if node.onboarding_percent is not None and d >= 40:
        layers.append(ft.Container(ft.ProgressRing(value=node.onboarding_percent / 100, width=d - 4, height=d - 4,
                                                   stroke_width=3, color=ft.Colors.with_opacity(0.9, ft.Colors.WHITE),
                                                   bgcolor=ft.Colors.with_opacity(0.25, ft.Colors.BLACK)),
                                   left=2, top=2))
    if node.needs_attention:
        layers.append(ft.Container(ft.Icon(ft.Icons.PRIORITY_HIGH, size=12, color=ft.Colors.ON_ERROR), width=18,
                                   height=18, border_radius=9, bgcolor=ft.Colors.ERROR, alignment=ft.Alignment.CENTER,
                                   right=0, top=0))
    if node.pinned:
        layers.append(ft.Container(ft.Icon(ft.Icons.PUSH_PIN, size=12, color=ft.Colors.ON_PRIMARY), width=18,
                                   height=18, border_radius=9, bgcolor=ft.Colors.PRIMARY, alignment=ft.Alignment.CENTER,
                                   left=0, top=0))
    return ft.Container(ft.Stack(layers, width=d, height=d), width=d, height=d, on_click=on_click, tooltip=tip,
                        border_radius=d / 2)


def legend() -> ft.Control:
    items = []
    for key in ("active", "slowing", "inactive", "new"):
        items.append(C.status_pill(key))
    items += [C.pill("Needs attention", bgcolor=ft.Colors.ERROR, color=ft.Colors.ON_ERROR, icon=ft.Icons.PRIORITY_HIGH),
              C.pill("In your focus", bgcolor=ft.Colors.PRIMARY, color=ft.Colors.ON_PRIMARY, icon=ft.Icons.PUSH_PIN),
              ft.Text("Ring = onboarding progress · Size = team size or weekly activity", size=12,
                      color=ft.Colors.ON_SURFACE_VARIANT)]
    return ft.Row(items, wrap=True, spacing=6, run_spacing=6)


def person_panel(S: AppState, n: teammap.MapNode, root_id: uuid.UUID, q: dict) -> ft.Control:
    note_tags = C.text_field("Private tags (comma separated)", value=", ".join(n.tags),
                             hint_text="e.g. ready to promote, needs coaching")
    note_text = C.text_field("Private note", multiline=True, min_lines=2, max_lines=6)

    @action
    async def load_note():
        note = await S.run(teammap.get_note, S.ctx, n.user_id)
        note_text.value = note.note if note and note.note else ""

    @action
    async def save_note(e):
        await S.run(teammap.save_note, S.ctx, n.user_id, note_tags.value.split(","), note_text.value)
        S.toast("Saved. Only you can see these notes.")

    @action
    async def toggle_pin(e):
        pinned = await S.run(teammap.toggle_pin, S.ctx, n.user_id)
        S.toast("Added to today's focus." if pinned else "Removed from focus.")
        await S.refresh()

    def share(e):
        title = C.text_field("Short label (no client names or details)", hint_text="e.g. Referral from the expo")

        async def submit():
            await S.run(teammap.share_opportunity, S.ctx, n.user_id, title.value)
            S.toast(f"Shared with {n.name}.")

        C.form_dialog(S.page, f"Share an opportunity with {n.name}", [
            ft.Text("Keep client details in your CRM. This records the hand-off so you can both track it.",
                    size=12, color=ft.Colors.ON_SURFACE_VARIANT), title], action(submit), "Share")

    S.page.run_task(load_note)
    issues = []
    if n.pending_signoffs:
        issues.append(f"{n.pending_signoffs} step(s) waiting for sign-off")
    if n.open_questions:
        issues.append(f"{n.open_questions} open question(s)")
    if n.overdue_steps:
        issues.append(f"{n.overdue_steps} overdue onboarding step(s)")
    params = f"size={q.get('size', 'team')}"
    return C.card(ft.Column([
        ft.Row([C.avatar(n.name, 22), ft.Column([ft.Text(n.name, weight=ft.FontWeight.W_700, size=16),
                                                  ft.Text((n.title or n.role.title()) + (f" · {n.team_name}" if n.team_name else ""),
                                                          size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
                                                 spacing=0, expand=True)]),
        ft.Row([C.status_pill(n.momentum)] + ([C.pill("In focus", icon=ft.Icons.PUSH_PIN)] if n.pinned else []), wrap=True),
        ft.Row([C.stat(str(n.week_activity), "this week"), C.stat(str(n.team_size), "in team")], spacing=8),
        ft.Row([C.stat(f"{n.onboarding_percent}%" if n.onboarding_percent is not None else "—", "onboarding"),
                C.stat(f"{n.days_idle}d" if n.days_idle is not None else "—", "since activity")], spacing=8),
        *[ft.Row([ft.Icon(ft.Icons.PRIORITY_HIGH, color=ft.Colors.ERROR, size=16), ft.Text(i, size=13)]) for i in issues],
        ft.Text(teammap.suggestion_for(n), italic=True),
        ft.Row([
            ft.FilledTonalButton("Unpin" if n.pinned else "Focus", icon=ft.Icons.PUSH_PIN_OUTLINED, on_click=toggle_pin),
            ft.OutlinedButton("Profile", icon=ft.Icons.PERSON_OUTLINE, on_click=lambda e: S.go(f"/people/{n.user_id}")),
        ], wrap=True),
        ft.Row([
            *([ft.OutlinedButton("Zoom into team", icon=ft.Icons.ZOOM_IN,
                                 on_click=lambda e: S.go(f"/team?root={n.user_id}&{params}"))] if n.children else []),
            ft.OutlinedButton("Share opportunity", icon=ft.Icons.HANDSHAKE_OUTLINED, on_click=share),
        ], wrap=True),
        ft.Divider(),
        ft.Text("Your private notes", theme_style=ft.TextThemeStyle.LABEL_LARGE),
        note_tags, note_text, ft.Row([ft.TextButton("Save notes", on_click=save_note)]),
    ], spacing=10), padding=16)


async def team_map_view(S: AppState, q: dict) -> ft.Control:
    root_id = uuid.UUID(q["root"]) if q.get("root") else None
    size_by = q.get("size", "team")
    mode = q.get("view", "map")
    root = await S.run(teammap.build_tree, S.ctx, root_id)
    everyone = teammap.flatten(root)
    selected_id = q.get("sel")
    selected = next((n for n in everyone if str(n.user_id) == selected_id), None)

    def link(**kw):
        params = {"root": q.get("root"), "size": size_by, "view": mode, "sel": q.get("sel")}
        params.update(kw)
        return "/team?" + "&".join(f"{k}={v}" for k, v in params.items() if v)

    toolbar = ft.Row([
        ft.SegmentedButton(selected=[mode], segments=[
            ft.Segment(value="map", label=ft.Text("Map"), icon=ft.Icon(ft.Icons.BUBBLE_CHART_OUTLINED)),
            ft.Segment(value="arrange", label=ft.Text("My layout"), icon=ft.Icon(ft.Icons.OPEN_WITH)),
            ft.Segment(value="list", label=ft.Text("List"), icon=ft.Icon(ft.Icons.LIST))],
            on_change=lambda e: S.go(link(view=e.control.selected[0]))),
        ft.SegmentedButton(selected=[size_by], segments=[
            ft.Segment(value="team", label=ft.Text("Size: team")),
            ft.Segment(value="activity", label=ft.Text("Size: activity"))],
            on_change=lambda e: S.go(link(size=e.control.selected[0])), visible=mode == "map"),
    ], wrap=True, spacing=10, run_spacing=8)

    crumbs: list[ft.Control] = []
    if root.user_id != S.user.id:
        crumbs = [ft.TextButton("My team", icon=ft.Icons.ARROW_BACK,
                                on_click=lambda e: S.go(link(root=None, sel=None))),
                  ft.Text(f"› {root.name}'s team", weight=ft.FontWeight.W_600)]

    if not everyone:
        body = C.card(ft.Column([
            C.empty("Your team map fills in as people join your team.", ft.Icons.BUBBLE_CHART_OUTLINED),
            ft.Row([ft.FilledButton("Invite someone", icon=ft.Icons.PERSON_ADD, on_click=lambda e: S.go("/people?invite=1"))],
                   alignment=ft.MainAxisAlignment.CENTER)]))
        return C.page_body(toolbar, body, title="Team map")

    content_w = min(C.MAX_WIDTH, (S.page.width or 1200) - (88 + 1 + 40 if S.is_wide else 40))
    panel_w = 320 if S.is_wide else 0
    W = max(280, min(720, content_w - panel_w - 24))

    def select(n):
        return lambda e: S.go(link(sel=str(n.user_id)))

    if mode == "list":
        rank = {"inactive": 0, "slowing": 1, "new": 2, "active": 3}
        rows = sorted(everyone, key=lambda n: (-(n.pending_signoffs + n.open_questions + n.overdue_steps),
                                               rank.get(n.momentum, 3), n.depth))
        table = ft.DataTable(columns=[ft.DataColumn(ft.Text(h)) for h in
                                      ("Person", "Status", "This week", "Onboarding", "Team", "Needs")],
                             rows=[ft.DataRow(cells=[
                                 ft.DataCell(ft.Row([ft.Container(width=12 * (n.depth - 1)), C.avatar(n.name, 14),
                                                     ft.Text(n.name)], spacing=6), on_tap=select(n)),
                                 ft.DataCell(C.status_pill(n.momentum)),
                                 ft.DataCell(ft.Text(str(n.week_activity))),
                                 ft.DataCell(ft.Text(f"{n.onboarding_percent}%" if n.onboarding_percent is not None else "—")),
                                 ft.DataCell(ft.Text(str(n.team_size))),
                                 ft.DataCell(ft.Text(teammap.suggestion_for(n) if n.needs_attention else "", size=12)),
                             ]) for n in rows])
        canvas = C.card(ft.Row([table], scroll=ft.ScrollMode.AUTO), padding=4)
    elif mode == "arrange":
        layout = await S.run(teammap.get_layout, S.ctx)
        positions = dict(layout.get("positions") or {})
        shown = [n for n in everyone if n.depth <= 3]
        d = 64 if len(shown) <= 30 else 48
        holders = {}
        stack_children = []
        cols = max(1, int(W // (d + 24)))
        for i, n in enumerate(shown):
            key = str(n.user_id)
            x, y = positions.get(key, [((i % cols) * (d + 24) + 12) / W, ((i // cols) * (d + 34) + 12) / W])
            gd = ft.GestureDetector(
                content=ft.Column([_bubble(n, d, False, selected is n, select(n)),
                                   ft.Text(n.name.split()[0], size=11, width=d, text_align=ft.TextAlign.CENTER,
                                           max_lines=1)], spacing=2, tight=True),
                left=x * W, top=y * W, drag_interval=40, mouse_cursor=ft.MouseCursor.MOVE, data=key)

            def on_drag(e: ft.DragUpdateEvent, gd=gd):
                gd.left = max(0, min(W - d, (gd.left or 0) + e.local_delta.x))
                gd.top = max(0, min(W - d, (gd.top or 0) + e.local_delta.y))
                gd.update()

            gd.on_pan_update = on_drag
            holders[key] = gd
            stack_children.append(gd)

        @action
        async def save(e):
            pos = {k: [(g.left or 0) / W, (g.top or 0) / W] for k, g in holders.items()}
            await S.run(teammap.save_layout, S.ctx, {"positions": pos, "groups": layout.get("groups") or {}})
            S.toast("Layout saved. Only you see this arrangement.")

        @action
        async def reset(e):
            await S.run(teammap.save_layout, S.ctx, {"positions": {}, "groups": {}})
            await S.refresh()

        canvas = ft.Column([
            ft.Text("Drag people to arrange your organization your way — for example, group the people you're "
                    "coaching this month. Your layout is private.", size=13, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Container(ft.Stack(stack_children, width=W, height=W), width=W, height=W, border_radius=16,
                         bgcolor=ft.Colors.SURFACE_CONTAINER_LOW, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)),
            ft.Row([ft.FilledButton("Save layout", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
                    ft.TextButton("Reset", on_click=reset)]),
        ], spacing=10)
    else:
        bubbles = teammap.pack(root, size_by=size_by, max_depth=2)
        stack_children = []
        for b in bubbles:
            d = max(14.0, 2 * b.r * W)
            is_enclosure = bool(b.node.children) and b.level == 1
            ctl = _bubble(b.node, d, is_enclosure, selected is b.node, select(b.node))
            ctl.left = b.x * W - d / 2
            ctl.top = b.y * W - d / 2
            ctl.animate_position = ft.Animation(300, ft.AnimationCurve.EASE_OUT)
            stack_children.append(ctl)
        canvas = ft.Column([
            ft.Container(ft.InteractiveViewer(ft.Stack(stack_children, width=W, height=W), min_scale=0.6, max_scale=4,
                                              boundary_margin=ft.Margin.all(40)),
                         width=W, height=W, border_radius=16, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                         border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), clip_behavior=ft.ClipBehavior.HARD_EDGE),
            legend(),
        ], spacing=10)

    panel = person_panel(S, selected, root.user_id, q) if selected else C.card(ft.Column([
        ft.Text("Tap a person", weight=ft.FontWeight.W_600),
        ft.Text("See how they're doing, add them to today's focus, keep private notes, or share an opportunity.",
                color=ft.Colors.ON_SURFACE_VARIANT),
        ft.Divider(),
        ft.Text(f"{root.team_size} people in {'your' if root.user_id == S.user.id else root.name + chr(39) + 's'} team",
                weight=ft.FontWeight.W_500),
        *[ft.Text(f"{LABELS[k]}: {sum(1 for n in everyone if n.momentum == k)}") for k in ("active", "slowing", "inactive", "new")],
    ], spacing=6))
    if S.is_wide:
        main = ft.Row([ft.Container(canvas, width=W), ft.Container(panel, width=panel_w)],
                      vertical_alignment=ft.CrossAxisAlignment.START, spacing=16)
    else:
        main = ft.Column([canvas, panel], spacing=16)

    async def live(event):
        if S.page.route.startswith("/team?") or S.page.route == "/team":
            await S.refresh()

    S.live_handlers["activity"] = S.live_handlers["progress"] = live
    return C.page_body(toolbar, *([ft.Row(crumbs)] if crumbs else []), main, title="Team map",
                       subtitle="Where to focus today",
                       actions=[ft.TextButton("Team progress", icon=ft.Icons.TASK_ALT, on_click=lambda e: S.go("/team-progress")),
                                ft.TextButton("Opportunities", icon=ft.Icons.HANDSHAKE_OUTLINED,
                                              on_click=lambda e: S.go("/opportunities"))])


# ------------------------------------------------------------ person profile

def activity_bars(days: list) -> ft.Control:
    peak = max([n for _, n in days] + [1])
    bars = []
    for d, n in days:
        bars.append(ft.Column([
            ft.Container(width=14, height=max(2, 70 * n / peak), border_radius=4,
                         bgcolor=ft.Colors.PRIMARY if n else ft.Colors.OUTLINE_VARIANT, tooltip=f"{d:%a %b %d}: {n}"),
            ft.Text(d.strftime("%a")[0], size=10, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=2, alignment=ft.MainAxisAlignment.END, horizontal_alignment=ft.CrossAxisAlignment.CENTER))
    return ft.Container(ft.Row(bars, spacing=6, vertical_alignment=ft.CrossAxisAlignment.END), height=96)


async def person_view(S: AppState, q: dict, user_id: str) -> ft.Control:
    uid = uuid.UUID(user_id)
    if uid == S.user.id:
        S.go("/")
        return None
    person = await S.run(orgs.get_person, S.ctx, uid)
    downline = S.ctx.is_admin or S.ctx.is_ancestor_of(person.path)
    header = ft.Row([C.avatar(person.name, 28), ft.Column([
        ft.Text(person.name, theme_style=ft.TextThemeStyle.HEADLINE_SMALL, weight=ft.FontWeight.W_600),
        ft.Text(f"{person.title or person.role.title()} · {person.email}", color=ft.Colors.ON_SURFACE_VARIANT,
                selectable=True)], spacing=0, expand=True)])
    if not downline:
        return C.page_body(header, C.card(ft.Text("This person is above you in your organization. "
                                                  "You can message them through Questions or your weekly update.")))
    tiles, days, gl, enr, ups, pending = await asyncio.gather(
        S.run(activity.tiles, S.ctx, uid), S.run(activity.daily_totals, S.ctx, uid, 14), S.run(goals.list_goals, S.ctx, uid),
        S.run(paths.person_enrollments, S.ctx, uid), S.run(updates.person_updates, S.ctx, uid),
        S.run(paths.pending_signoffs, S.ctx))
    blocks: list[ft.Control] = [header]
    if S.ctx.activity_visible_upline or S.ctx.is_admin:
        blocks.append(C.card(C.section("Activity (last 14 days)", activity_bars(days), ft.ResponsiveRow([
            ft.Container(C.stat(f"{t.week}", f"{t.label} this week"), col={"xs": 6, "md": 3}) for t in tiles],
            spacing=8, run_spacing=8))))
    else:
        blocks.append(C.card(ft.Text("Daily activity is shared through weekly updates in this organization.",
                                     color=ft.Colors.ON_SURFACE_VARIANT)))

    mine_pending = [row for row in pending if row[0].user_id == uid]

    @action
    async def signoff(e):
        approve, pid = e.control.data
        await S.run(paths.sign_off, S.ctx, pid, approve)
        await S.refresh()

    if mine_pending:
        blocks.append(C.card(C.section("Waiting for your sign-off", *[ft.ListTile(
            title=ft.Text(step.title), subtitle=ft.Text(f"{path_title}" + (f" · note: {sp.note}" if sp.note else "")),
            trailing=ft.Row([ft.TextButton("Return", data=(False, sp.id), on_click=signoff),
                             ft.FilledButton("Sign off", data=(True, sp.id), on_click=signoff)], tight=True))
            for sp, step, _name, path_title in mine_pending])))
    blocks.append(C.section("Goals", *[goal_card(S, g, True, False) for g in gl] or [ft.Text("No goals yet.")],
                            trailing=ft.TextButton("Set a goal", icon=ft.Icons.ADD,
                                                   on_click=lambda e: new_goal_dialog(S, uid, person.name))))
    assign = ft.TextButton("Assign a path", icon=ft.Icons.ADD, on_click=lambda e: assign_dialog(S, [uid]),
                           visible=S.ctx.is_author)
    blocks.append(C.section("Onboarding", *[enrollment_card(S, e, clickable=False) for e in enr]
                            or [ft.Text("No paths assigned.")], trailing=assign))
    blocks.append(C.section("Weekly updates", *[ft.ListTile(
        leading=ft.Icon(ft.Icons.EVENT_NOTE_OUTLINED), title=ft.Text(f"Week of {u.period_start:%b %d}"),
        subtitle=ft.Text((u.wins or "")[:120]), on_click=lambda e, i=u.id: S.go(f"/updates/{i}")) for u in ups]
        or [ft.Text("No updates yet.")]))
    return C.page_body(*blocks)


def assign_dialog(S: AppState, user_ids: list[uuid.UUID]) -> None:
    async def open_it():
        options = [p for p in await S.run(paths.list_paths, S.ctx) if p.published_version]
        if not options:
            S.toast("Publish a path first (Paths → open a path → Publish).", error=True)
            return
        dd = ft.Dropdown(label="Path", border_radius=10, value=str(options[0].id),
                         options=[ft.DropdownOption(key=str(p.id), text=p.title) for p in options])

        async def submit():
            n = await S.run(paths.enroll, S.ctx, uuid.UUID(dd.value), user_ids)
            S.toast("Assigned." if n else "Already assigned.")
            await S.refresh()

        C.form_dialog(S.page, "Assign an onboarding path", [dd], action(submit), "Assign")

    S.page.run_task(action(open_it))


# ------------------------------------------------------------ team progress

async def team_progress_view(S: AppState, q: dict) -> ft.Control:
    pending, enrollments, goal_queue = await asyncio.gather(
        S.run(paths.pending_signoffs, S.ctx), S.run(paths.team_enrollments, S.ctx), S.run(goals.pending_approvals, S.ctx))

    @action
    async def signoff(e):
        approve, pid = e.control.data
        await S.run(paths.sign_off, S.ctx, pid, approve)
        S.toast("Signed off." if approve else "Returned for another look.")
        await S.refresh()

    @action
    async def migrate(e):
        await S.run(paths.migrate_enrollment, S.ctx, e.control.data)
        S.toast("Moved to the latest version. Completed steps were kept.")
        await S.refresh()

    sign_rows = [ft.ListTile(
        leading=C.avatar(name), title=ft.Text(f"{name} — {step.title}"),
        subtitle=ft.Text(f"{path_title} · completed {C.when(sp.completed_at)}" + (f" · “{sp.note}”" if sp.note else "")),
        trailing=ft.Row([ft.TextButton("Return", data=(False, sp.id), on_click=signoff),
                         ft.FilledButton("Sign off", data=(True, sp.id), on_click=signoff)], tight=True))
        for sp, step, name, path_title in pending]
    rows = []
    for e in sorted(enrollments, key=lambda e: e.percent):
        outdated = e.latest_version_no and e.latest_version_no > e.version_no and S.ctx.is_author
        overdue = sum(1 for s in e.steps if s.overdue)
        rows.append(ft.ListTile(
            leading=C.avatar(e.user_name or "?"), title=ft.Text(f"{e.user_name} · {e.title}"),
            subtitle=ft.Column([ft.ProgressBar(value=e.percent / 100, bar_height=6),
                                ft.Text(f"{e.done}/{e.total} done" + (f" · {overdue} overdue" if overdue else "")
                                        + (f" · on v{e.version_no}, latest v{e.latest_version_no}" if outdated else ""),
                                        size=12)], spacing=4),
            trailing=ft.TextButton("Update version", data=e.id, on_click=migrate) if outdated else None,
            on_click=lambda ev, u=e.user_id: S.go(f"/people/{u}")))
    from .goals_view import approval_card

    goal_block = [C.section("Goals waiting for approval", *[approval_card(S, g, name) for g, name in goal_queue])] \
        if goal_queue else []
    return C.page_body(*goal_block,
                       C.card(C.section("Waiting for sign-off", *(sign_rows or [ft.Text("Nothing waiting.")]))),
                       C.card(C.section("Onboarding in progress", *(rows or [ft.Text("No active onboarding in your team.")]))),
                       title="Team progress")


# ------------------------------------------------------------ opportunities

async def opportunities_view(S: AppState, q: dict) -> ft.Control:
    rows, mates = await asyncio.gather(S.run(teammap.list_opportunities, S.ctx), S.run(teammap.teammates, S.ctx))

    def share(e):
        if not mates:
            S.toast("There's no one to share with yet.", error=True)
            return
        to = ft.Dropdown(label="Teammate", border_radius=10, enable_filter=True,
                         options=[ft.DropdownOption(key=str(u), text=n) for u, n in mates])
        title = C.text_field("Short label (no client names or details)")

        async def submit():
            if not to.value:
                S.toast("Choose a teammate.", error=True)
                return False
            await S.run(teammap.share_opportunity, S.ctx, uuid.UUID(to.value), title.value)
            await S.refresh()

        C.form_dialog(S.page, "Share an opportunity", [to, title], action(submit), "Share")

    @action
    async def set_status(e):
        oid, status = e.control.data
        await S.run(teammap.update_opportunity, S.ctx, oid, status)
        await S.refresh()

    items = []
    for o, from_name, to_name in rows:
        incoming = o.to_user_id == S.user.id
        buttons = []
        if incoming and o.status == "shared":
            buttons.append(ft.FilledTonalButton("Accept", data=(o.id, "accepted"), on_click=set_status))
        if o.status in ("shared", "accepted"):
            buttons += [ft.TextButton("Won", data=(o.id, "won"), on_click=set_status),
                        ft.TextButton("Lost", data=(o.id, "lost"), on_click=set_status)]
        items.append(C.card(ft.ListTile(
            leading=ft.Icon(ft.Icons.CALL_RECEIVED if incoming else ft.Icons.CALL_MADE),
            title=ft.Text(o.title, weight=ft.FontWeight.W_500),
            subtitle=ft.Text(f"{'From ' + from_name if incoming else 'To ' + to_name} · {C.when(o.created_at)} · {o.status}"),
            trailing=ft.Row(buttons, tight=True) if buttons else C.pill(o.status.title())), padding=4))
    return C.page_body(*items or [C.empty("Opportunities you share with teammates — and ones shared with you — show up here.",
                                          ft.Icons.HANDSHAKE_OUTLINED)],
                       title="Opportunities", subtitle="Hand-offs between teammates (client details stay in your CRM)",
                       actions=[ft.FilledButton("Share an opportunity", icon=ft.Icons.ADD, on_click=share)])
