"""Small, reusable building blocks for views."""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

import flet as ft

from .theme import STATUS_COLORS, STATUS_ICONS

MAX_WIDTH = 1100


def page_body(*controls: ft.Control, title: str | None = None, subtitle: str | None = None,
              actions: list[ft.Control] | None = None) -> ft.Control:
    header = []
    if title:
        title_col = ft.Column([ft.Text(title, theme_style=ft.TextThemeStyle.HEADLINE_SMALL, weight=ft.FontWeight.W_600),
                               *([ft.Text(subtitle, color=ft.Colors.ON_SURFACE_VARIANT)] if subtitle else [])],
                              spacing=2)
        if actions:
            header.append(ft.ResponsiveRow([
                ft.Container(title_col, col={"xs": 12, "md": 7}),
                ft.Container(ft.Row(actions, spacing=8, wrap=True, alignment=ft.MainAxisAlignment.END),
                             col={"xs": 12, "md": 5}, alignment=ft.Alignment.TOP_RIGHT),
            ], run_spacing=8, vertical_alignment=ft.CrossAxisAlignment.START))
        else:
            header.append(title_col)
    return ft.Container(
        ft.Column([*header, *controls], spacing=18, width=MAX_WIDTH),
        alignment=ft.Alignment.TOP_CENTER, padding=ft.Padding.symmetric(horizontal=20, vertical=20),
    )


def section(title: str, *controls: ft.Control, trailing: ft.Control | None = None) -> ft.Control:
    return ft.Column([
        ft.Row([ft.Text(title, theme_style=ft.TextThemeStyle.TITLE_MEDIUM, weight=ft.FontWeight.W_600, expand=True),
                *([trailing] if trailing else [])]),
        *controls,
    ], spacing=10)


def card(content: ft.Control, padding: int = 16, on_click: Callable | None = None, **kw) -> ft.Control:
    return ft.Card(ft.Container(content, padding=padding, on_click=on_click, border_radius=12), **kw)


def empty(message: str, icon=ft.Icons.INBOX_OUTLINED) -> ft.Control:
    return ft.Container(
        ft.Column([ft.Icon(icon, size=36, color=ft.Colors.OUTLINE), ft.Text(message, color=ft.Colors.ON_SURFACE_VARIANT,
                                                                              text_align=ft.TextAlign.CENTER)],
                  horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        padding=24, alignment=ft.Alignment.CENTER,
    )


def initials(name: str) -> str:
    parts = [p for p in (name or "?").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def avatar(name: str, radius: int = 18, color: str | None = None) -> ft.Control:
    return ft.CircleAvatar(content=ft.Text(initials(name), size=radius * 0.75, weight=ft.FontWeight.W_600),
                           radius=radius, bgcolor=color or ft.Colors.PRIMARY_CONTAINER,
                           color=ft.Colors.ON_PRIMARY_CONTAINER)


def pill(text: str, bgcolor=ft.Colors.SECONDARY_CONTAINER, color=ft.Colors.ON_SECONDARY_CONTAINER, icon=None) -> ft.Control:
    return ft.Container(
        ft.Row([*([ft.Icon(icon, size=14, color=color)] if icon else []), ft.Text(text, size=12, color=color,
                                                                                   weight=ft.FontWeight.W_500)],
               spacing=4, tight=True),
        bgcolor=bgcolor, border_radius=20, padding=ft.Padding.symmetric(horizontal=10, vertical=3),
    )


def status_pill(status: str) -> ft.Control:
    fill, text = STATUS_COLORS.get(status, STATUS_COLORS["new"])
    labels = {"active": "Active", "slowing": "Slowing", "inactive": "Inactive", "new": "New"}
    return pill(labels.get(status, status.title()), bgcolor=fill, color=text, icon=STATUS_ICONS.get(status))


def progress(value: float | None, label: str | None = None) -> ft.Control:
    v = max(0.0, min(1.0, value or 0.0))
    return ft.Column([
        *([ft.Row([ft.Text(label, size=12, color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                   ft.Text(f"{round(v * 100)}%", size=12, weight=ft.FontWeight.W_600)])] if label else []),
        ft.ProgressBar(value=v, bar_height=8, border_radius=8),
    ], spacing=4)


def stat(value: str, label: str, icon=None) -> ft.Control:
    return ft.Container(
        ft.Column([
            ft.Row([*([ft.Icon(icon, size=18, color=ft.Colors.PRIMARY)] if icon else []),
                    ft.Text(value, size=22, weight=ft.FontWeight.W_700)], spacing=6),
            ft.Text(label, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=2),
        padding=12, border_radius=12, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH, expand=True,
    )


def text_field(label: str, value: str = "", **kw) -> ft.TextField:
    """Full-width by default (inside columns); pass width= or expand= when placing in a Row."""
    if "width" not in kw and "expand" not in kw:
        kw["width"] = float("inf")
    return ft.TextField(label=label, value=value, border_radius=10, **kw)


def when(dt: datetime | date | None) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        delta = datetime.now(dt.tzinfo) - dt
        if delta.days == 0:
            mins = int(delta.total_seconds() // 60)
            if mins < 1:
                return "just now"
            if mins < 60:
                return f"{mins} min ago"
            return f"{mins // 60} h ago"
        if delta.days == 1:
            return "yesterday"
        if delta.days < 7:
            return f"{delta.days} days ago"
        return dt.strftime("%b %d")
    return dt.strftime("%b %d")


def confirm(page: ft.Page, title: str, message: str, on_yes: Callable, yes_label: str = "Confirm",
            danger: bool = False) -> None:
    async def yes(e):
        page.pop_dialog()
        await on_yes()

    page.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text(title), content=ft.Text(message),
        actions=[ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
                 ft.FilledButton(yes_label, on_click=yes,
                                 style=ft.ButtonStyle(bgcolor=ft.Colors.ERROR) if danger else None)],
    ))


def form_dialog(page: ft.Page, title: str, fields: list[ft.Control], on_submit: Callable, submit_label: str = "Save",
                width: int = 480) -> ft.AlertDialog:
    async def submit(e):
        ok = await on_submit()
        if ok is not False:
            page.pop_dialog()

    dlg = ft.AlertDialog(
        modal=True, title=ft.Text(title),
        content=ft.Container(ft.Column(fields, tight=True, spacing=12, scroll=ft.ScrollMode.AUTO), width=width),
        actions=[ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
                 ft.FilledButton(submit_label, on_click=submit)],
    )
    page.show_dialog(dlg)
    return dlg


def split(markdown_text: str | None) -> ft.Control:
    return ft.Markdown(markdown_text or "", selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                       auto_follow_links=False)


def banner(icon, title: str, subtitle: str, actions: list[ft.Control], subtitle_color=None) -> ft.Control:
    """Card with an icon, two lines of text and buttons that move below the text on narrow screens.
    (A Row with wrap=True can't contain expanding children, so this uses a ResponsiveRow.)"""
    return card(ft.ResponsiveRow([
        ft.Container(ft.Row([
            ft.Icon(icon, color=ft.Colors.PRIMARY, size=30),
            ft.Column([ft.Text(title, weight=ft.FontWeight.W_600),
                       ft.Text(subtitle, size=13, color=subtitle_color or ft.Colors.ON_SURFACE_VARIANT)],
                      spacing=2, expand=True),
        ]), col={"xs": 12, "md": 7}),
        ft.Container(ft.Row(actions, spacing=8, wrap=True, alignment=ft.MainAxisAlignment.END),
                     col={"xs": 12, "md": 5}, alignment=ft.Alignment.CENTER_RIGHT),
    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, run_spacing=8))
