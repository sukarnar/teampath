"""Flet entry point: session setup, routing and the app shell."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qs, urlparse

import flet as ft

from .. import events
from ..services import notify
from . import components as C
from .state import AppState, action, register_state, unregister_state
from .theme import apply_theme
from .views import (
    auth_views,
    goals_view,
    home,
    learn,
    library,
    notes,
    org_admin,
    paths_admin,
    people,
    platform_view,
    prospects,
    questions,
    security_view,
    tasks,
    team,
    updates_view,
)

log = logging.getLogger("teampath.ui")
UUID = r"[0-9a-fA-F-]{36}"

# (pattern, view, level) — level: public | user (signed in) | org (inside an organization)
ROUTES = [
    (r"^/login$", auth_views.login_view, "public"),
    (r"^/mfa$", auth_views.mfa_view, "public"),
    (r"^/signup$", auth_views.signup_view, "public"),
    (r"^/forgot$", auth_views.forgot_view, "public"),
    (r"^/reset$", auth_views.reset_view, "public"),
    (r"^/invite$", auth_views.invite_view, "public"),
    (r"^/orgs$", auth_views.orgs_view, "user"),
    (r"^/security$", security_view.security_view, "user"),
    (r"^/platform$", platform_view.platform_view, "user"),
    (r"^/$", home.home_view, "org"),
    (r"^/learn$", learn.learn_view, "org"),
    (rf"^/learn/step/(?P<step_id>{UUID})$", learn.step_view, "org"),
    (rf"^/content/(?P<item_id>{UUID})$", learn.content_view, "org"),
    (r"^/goals$", goals_view.goals_view, "org"),
    (r"^/updates$", updates_view.updates_view, "org"),
    (rf"^/updates/(?P<update_id>{UUID})$", updates_view.update_detail_view, "org"),
    (r"^/questions$", questions.questions_view, "org"),
    (rf"^/questions/(?P<thread_id>{UUID})$", questions.thread_view, "org"),
    (r"^/team$", team.team_map_view, "org"),
    (r"^/team-progress$", team.team_progress_view, "org"),
    (r"^/opportunities$", team.opportunities_view, "org"),
    (r"^/people$", people.people_view, "org"),
    (rf"^/people/(?P<user_id>{UUID})$", team.person_view, "org"),
    (r"^/prospects$", prospects.prospects_view, "org"),
    (rf"^/prospects/(?P<prospect_id>{UUID})$", prospects.detail_view, "org"),
    (r"^/prospects/list/(?P<list_id>[A-Za-z_]+|[0-9a-fA-F-]{36})$", prospects.list_view, "org"),
    (rf"^/prospects/focus/(?P<list_id>{UUID})$", prospects.focus_view, "org"),
    (r"^/tasks$", tasks.tasks_view, "org"),
    (r"^/tasks/assistants$", tasks.assistants_view, "org"),
    (rf"^/tasks/(?P<task_id>{UUID})$", tasks.task_view, "org"),
    (r"^/notes$", notes.notes_view, "org"),
    (r"^/notes/takeaways$", notes.takeaways_view, "org"),
    (rf"^/notes/events/(?P<event_id>{UUID})$", notes.event_view, "org"),
    (rf"^/notes/shared/(?P<shared_id>{UUID})$", notes.shared_view, "org"),
    (rf"^/notes/(?P<note_id>{UUID})$", notes.note_view, "org"),
    (r"^/library$", library.library_view, "org"),
    (r"^/paths$", paths_admin.paths_view, "org"),
    (rf"^/paths/(?P<path_id>{UUID})$", paths_admin.path_editor_view, "org"),
    (r"^/org$", org_admin.org_view, "org"),
]


def nav_items(S: AppState) -> list[tuple[str, str, object]]:
    if S.ctx and S.ctx.is_assistant:
        return [("/", "Today", ft.Icons.TODAY_OUTLINED), ("/tasks", "Tasks", ft.Icons.CHECKLIST),
                ("/learn", "Learn", ft.Icons.SCHOOL_OUTLINED), ("/notes", "Notes", ft.Icons.EDIT_NOTE),
                ("/library", "Library", ft.Icons.FOLDER_OUTLINED),
                ("/questions", "Questions", ft.Icons.HELP_OUTLINE), ("/people", "People", ft.Icons.GROUPS_OUTLINED)]
    items = [("/", "Today", ft.Icons.TODAY_OUTLINED), ("/prospects", "Prospects", ft.Icons.CONTACT_PHONE_OUTLINED),
             ("/tasks", "Tasks", ft.Icons.CHECKLIST), ("/notes", "Notes", ft.Icons.EDIT_NOTE),
             ("/learn", "Learn", ft.Icons.SCHOOL_OUTLINED), ("/goals", "Goals", ft.Icons.FLAG_OUTLINED), ("/updates", "Updates", ft.Icons.FORUM_OUTLINED),
             ("/questions", "Questions", ft.Icons.HELP_OUTLINE)]
    if S.has_team():
        items.append(("/team", "Team", ft.Icons.BUBBLE_CHART_OUTLINED))
    items += [("/library", "Library", ft.Icons.FOLDER_OUTLINED), ("/people", "People", ft.Icons.GROUPS_OUTLINED)]
    if S.ctx and S.ctx.is_author:
        items.append(("/paths", "Paths", ft.Icons.ROUTE_OUTLINED))
    if S.ctx and S.ctx.is_admin:
        items.append(("/org", "Organization", ft.Icons.APARTMENT_OUTLINED))
    return items


def _selected(items, route: str) -> int:
    best, best_len = 0, -1
    for i, (r, _, _) in enumerate(items):
        if (route == r or (r != "/" and route.startswith(r))) and len(r) > best_len:
            best, best_len = i, len(r)
    if route.startswith("/team-progress") or route.startswith("/opportunities"):
        return next((i for i, it in enumerate(items) if it[0] == "/team"), best)
    return best


def brand_header(S: AppState, compact=False) -> ft.Control:
    p = S.profile
    logo = (ft.Image(src=S.logo_url, height=36, width=36, fit=ft.BoxFit.CONTAIN, border_radius=8)
            if S.logo_url else C.avatar(p.name if p else "?", radius=18, color=ft.Colors.PRIMARY))
    lines = [ft.Text(p.name if p else "", weight=ft.FontWeight.W_700, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)]
    if p and p.team_name:
        lines.append(ft.Row([*([ft.Image(src=S.team_logo_url, height=14, width=14)] if S.team_logo_url else []),
                             ft.Text(p.team_name, size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                                     max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)], spacing=4))
    elif p and p.tagline and not compact:
        lines.append(ft.Text(p.tagline, size=12, color=ft.Colors.ON_SURFACE_VARIANT, max_lines=1,
                             overflow=ft.TextOverflow.ELLIPSIS))
    return ft.Row([logo, ft.Column(lines, spacing=0, expand=True)], spacing=10)


def account_menu(S: AppState) -> ft.Control:
    @action
    async def sign_out(e):
        await S.sign_out()

    items = [ft.PopupMenuItem(content=ft.Text(S.user.email, size=12, color=ft.Colors.ON_SURFACE_VARIANT), disabled=True),
             ft.PopupMenuItem(content=ft.Text("Security & sessions"), icon=ft.Icons.SHIELD_OUTLINED,
                              on_click=lambda e: S.go("/security"))]
    items.append(ft.PopupMenuItem(content=ft.Text("Switch organization"), icon=ft.Icons.SWAP_HORIZ,
                                  on_click=lambda e: S.go("/orgs")))
    if S.user.is_platform_admin:
        items.append(ft.PopupMenuItem(content=ft.Text("Platform admin"), icon=ft.Icons.ADMIN_PANEL_SETTINGS_OUTLINED,
                                      on_click=lambda e: S.go("/platform")))
    items.append(ft.PopupMenuItem(content=ft.Text("Sign out"), icon=ft.Icons.LOGOUT, on_click=sign_out))
    return ft.PopupMenuButton(content=C.avatar(S.user.name, radius=16), items=items, tooltip="Account")


def notifications_button(S: AppState, count: int) -> ft.Control:
    badge_btn = ft.IconButton(ft.Icons.NOTIFICATIONS_OUTLINED, tooltip="Notifications",
                              badge=ft.Badge(label=str(count)) if count else None)

    @action
    async def open_list(e):
        items = await S.run(notify.my_notifications, S.ctx)
        await S.run(notify.mark_all_read, S.ctx)
        badge_btn.badge = None
        badge_btn.update()

        def go(route):
            S.page.pop_dialog()
            if route:
                S.go(route)

        tiles = [ft.ListTile(
            leading=ft.Icon(ft.Icons.CIRCLE, size=10, color=ft.Colors.PRIMARY if not n.read_at else ft.Colors.TRANSPARENT),
            title=ft.Text(n.title), subtitle=ft.Text(C.when(n.created_at) + (f" · {n.body}" if n.body else ""),
                                                    max_lines=2),
            on_click=lambda e, r=n.route: go(r)) for n in items]
        S.page.show_dialog(ft.AlertDialog(
            title=ft.Text("Notifications"),
            content=ft.Container(ft.Column(tiles or [C.empty("You're all caught up.")], scroll=ft.ScrollMode.AUTO,
                                           tight=True), width=460, height=420 if tiles else None),
            actions=[ft.TextButton("Close", on_click=lambda e: S.page.pop_dialog())]))

    badge_btn.on_click = open_list

    async def on_live(event):
        n = await asyncio.to_thread(notify.unread_count, S.ctx)
        badge_btn.badge = ft.Badge(label=str(n)) if n else None
        badge_btn.update()

    S.live_handlers["notification"] = on_live
    return badge_btn


def shell(S: AppState, body: ft.Control, route: str, unread: int) -> ft.Control:
    items = nav_items(S)
    sel = _selected(items, route)
    scroll_body = ft.Column([body], scroll=ft.ScrollMode.AUTO, expand=True)

    def on_nav(e):
        idx = int(e.control.selected_index)
        S.go(items[idx][0])

    if S.is_wide:
        rail = ft.NavigationRail(
            selected_index=sel, label_type=ft.NavigationRailLabelType.ALL, min_width=88,
            destinations=[ft.NavigationRailDestination(icon=i, label=l) for _, l, i in items],
            on_change=on_nav, group_alignment=-0.9,
        )
        top = ft.Container(ft.Row([ft.Container(brand_header(S), expand=True), notifications_button(S, unread),
                                   account_menu(S)], spacing=6),
                           padding=ft.Padding.symmetric(horizontal=16, vertical=8),
                           border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)))
        return ft.Row([rail, ft.VerticalDivider(width=1),
                       ft.Column([top, scroll_body], expand=True, spacing=0)], expand=True, spacing=0)
    primary = items[:4]
    more = items[4:]
    bar_sel = sel if sel < 4 else 4

    def on_bar(e):
        idx = int(e.control.selected_index)
        if idx < 4:
            S.go(primary[idx][0])
        else:
            S.page.show_dialog(ft.BottomSheet(ft.Container(ft.Column(
                [ft.ListTile(leading=ft.Icon(i), title=ft.Text(l), on_click=lambda e, r=r: (S.page.pop_dialog(), S.go(r)))
                 for r, l, i in more], tight=True), padding=12)))

    bar = ft.NavigationBar(selected_index=bar_sel, on_change=on_bar, destinations=[
        *[ft.NavigationBarDestination(icon=i, label=l) for _, l, i in primary],
        ft.NavigationBarDestination(icon=ft.Icons.MENU, label="More")])
    top = ft.Container(ft.Row([ft.Container(brand_header(S, compact=True), expand=True),
                               notifications_button(S, unread), account_menu(S)], spacing=4),
                       padding=ft.Padding.symmetric(horizontal=12, vertical=6))
    return ft.Column([top, ft.Divider(height=1), scroll_body, bar], expand=True, spacing=0)


def user_shell(S: AppState, body: ft.Control) -> ft.Control:
    """Frame for signed-in pages outside an organization (org picker, security, platform)."""
    actions = [ft.TextButton("Back to app", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/"))] if S.ctx else []

    @action
    async def sign_out(e):
        await S.sign_out()

    top = ft.Container(ft.Row([ft.Text(S.settings.app_name, weight=ft.FontWeight.W_700, size=18, expand=True), *actions,
                               ft.TextButton("Sign out", icon=ft.Icons.LOGOUT, on_click=sign_out)]),
                       padding=ft.Padding.symmetric(horizontal=16, vertical=8))
    return ft.Column([top, ft.Divider(height=1), ft.Column([body], scroll=ft.ScrollMode.AUTO, expand=True)],
                     expand=True, spacing=0)


async def render(S: AppState) -> None:
    from . import state as _state

    _state.INFLIGHT += 1
    try:
        await _render(S)
    finally:
        _state.INFLIGHT -= 1


async def _render(S: AppState) -> None:
    page = S.page
    parsed = urlparse(page.route or "/")
    path = parsed.path or "/"
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    S.live_handlers = {}
    S.refresh = lambda: render(S)

    if S.user and S.session_expired():
        await S.sign_out("You were signed out after a period of inactivity.")
        return
    S.touch()

    for pattern, view, level in ROUTES:
        m = re.match(pattern, path)
        if not m:
            continue
        if level != "public" and not S.user:
            S.after_login_route = page.route if level == "org" and path != "/" else None
            S.go("/login")
            return
        if level == "org" and not S.ctx:
            S.go("/orgs")
            return
        if S.ctx and S.extra("mfa_needed") and path != "/security":
            S.go("/security")
            return
        try:
            body = await view(S, query, **m.groupdict())
        except Exception as e:
            from .state import USER_ERRORS, Abort

            if not isinstance(e, (Abort, *USER_ERRORS)):
                log.exception("view failed: %s", path)
            body = C.page_body(C.empty("This page isn't available.", ft.Icons.ERROR_OUTLINE),
                               ft.Row([ft.FilledButton("Go to Today", on_click=lambda e: S.go("/"))],
                                      alignment=ft.MainAxisAlignment.CENTER))
        if body is None:
            return
        if level == "org":
            unread = await asyncio.to_thread(notify.unread_count, S.ctx)
            page.controls = [shell(S, body, path, unread)]
        elif level == "user":
            page.controls = [user_shell(S, body)]
        else:
            page.controls = [body]
        page.update()
        return
    S.go("/" if S.user else "/login")


async def main(page: ft.Page):
    page.title = "Sign in"
    page.padding = 0
    page.spacing = 0
    apply_theme(page, None)
    S = AppState(page)
    register_state(S)
    S.prefs = ft.SharedPreferences()
    S.file_picker = ft.FilePicker(on_upload=S.on_upload)

    async def on_route_change(e=None):
        await render(S)

    async def on_resize(e):
        if S.ctx and (page.width or 0) and (S.is_wide != getattr(S, "_was_wide", None)):
            S._was_wide = S.is_wide
            await render(S)

    def on_close(e):
        events.unsubscribe_all(S.sub_key)
        unregister_state(S)

    page.on_route_change = on_route_change
    page.on_resize = on_resize
    page.on_close = on_close
    S._was_wide = S.is_wide

    async def watchdog():
        n = 0
        while True:
            await asyncio.sleep(30)
            n += 1
            try:
                if S.user and S.session_expired():
                    await S.sign_out("You were signed out after a period of inactivity.")
                elif S.user and n % 4 == 0 and not await S.check_token():
                    await S.sign_out("You were signed out from another device.")
            except Exception:
                return  # session closed

    page.run_task(watchdog)

    start = page.route or "/"
    public = start.split("?")[0] in ("/invite", "/reset", "/signup", "/forgot")
    S.refresh = lambda: render(S)
    if not public and await S.try_resume():
        if start not in ("/", "/login", "/orgs"):
            S.after_login_route = start
        await S.choose_org_automatically()
        return
    await render(S)
