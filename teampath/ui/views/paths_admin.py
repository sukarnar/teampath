"""Authoring onboarding paths: steps, versions, publishing and assigning."""

from __future__ import annotations

import uuid
from itertools import groupby

import flet as ft

from ...services import content, orgs, paths
from .. import components as C
from ..state import AppState, action
from .learn import STEP_ICONS

STEP_TYPES = [("task", "Task"), ("read", "Read / watch material"), ("acknowledge", "Acknowledge a policy"),
              ("meet", "Meet someone")]


async def paths_view(S: AppState, q: dict) -> ft.Control:
    S.ctx.require_author()
    items = await S.run(paths.list_paths, S.ctx)

    def create(e):
        title = C.text_field("Path title", hint_text="e.g. New agent — first 30 days")
        desc = C.text_field("Description (optional)", multiline=True, min_lines=2, max_lines=4)
        team_only = ft.Switch(label="Only for my team", value=not S.ctx.is_admin, disabled=not S.ctx.is_admin)

        async def submit():
            pid = await S.run(paths.create_path, S.ctx, title.value, desc.value, team_only.value)
            S.go(f"/paths/{pid}")

        C.form_dialog(S.page, "New onboarding path", [title, desc, team_only], action(submit), "Create")

    cards = []
    for p in items:
        status = f"Published v{p.published_version}" if p.published_version else "Draft — not published"
        if p.has_draft and p.published_version:
            status += " · unpublished changes"
        cards.append(C.card(ft.ListTile(
            leading=ft.Icon(ft.Icons.ROUTE, color=ft.Colors.PRIMARY), title=ft.Text(p.title, weight=ft.FontWeight.W_600),
            subtitle=ft.Text(f"{status} · {p.enrolled_count} active" + (" · team only" if p.scoped else "")),
            trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT) if p.can_edit else None,
            on_click=(lambda e, i=p.id: S.go(f"/paths/{i}")) if p.can_edit else None), padding=4))
    return C.page_body(*cards or [C.empty("Create your first onboarding path.", ft.Icons.ROUTE)],
                       title="Onboarding paths", subtitle="Step-by-step plans you assign to new people",
                       actions=[ft.FilledButton("New path", icon=ft.Icons.ADD, on_click=create)])


def step_dialog(S: AppState, path_id: uuid.UUID, library: list, step: paths.StepView | None = None,
                stages: list[str] | None = None) -> None:
    title = C.text_field("Step title", value=step.title if step else "")
    stage = C.text_field("Stage", value=step.stage if step else (stages[-1] if stages else "Week 1"),
                         hint_text="e.g. Day 1, Week 1, First 30 days")
    kind = ft.Dropdown(label="Type", value=step.step_type if step else "task", border_radius=10,
                       options=[ft.DropdownOption(key=k, text=v) for k, v in STEP_TYPES])
    instructions = C.text_field("Instructions (Markdown supported)", value=(step.instructions_md or "") if step else "",
                                multiline=True, min_lines=4, max_lines=12)
    material = ft.Dropdown(label="Material (optional)", border_radius=10, enable_filter=True,
                           value=str(step.content_item_id) if step and step.content_item_id else "none",
                           options=[ft.DropdownOption(key="none", text="— none —")] +
                           [ft.DropdownOption(key=str(i.id), text=i.title) for i in library])
    due = C.text_field("Due (days after start)", value=str(step.due_offset_days if step else 7),
                       keyboard_type=ft.KeyboardType.NUMBER, width=200)
    signoff = ft.Switch(label="Leader must sign off", value=step.requires_signoff if step else False)

    async def submit():
        try:
            days = int(due.value or 0)
        except ValueError:
            S.toast("Due must be a number of days.", error=True)
            return False
        await S.run(paths.save_step, S.ctx, path_id, title=title.value, stage=stage.value,
                    instructions_md=instructions.value, step_type=kind.value,
                    content_item_id=None if material.value in (None, "none") else uuid.UUID(material.value),
                    due_offset_days=days, requires_signoff=signoff.value, step_id=step.id if step else None)
        await S.refresh()

    C.form_dialog(S.page, "Edit step" if step else "Add a step", [title, stage, kind, instructions, material, due, signoff],
                  action(submit), width=600)


def people_picker(S: AppState, people: list[orgs.Person], on_done) -> None:
    boxes = [ft.Checkbox(label=("— " * p.depth) + p.name, data=p.user_id) for p in people if p.user_id != S.user.id]

    async def submit():
        chosen = [b.data for b in boxes if b.value]
        if not chosen:
            S.toast("Choose at least one person.", error=True)
            return False
        await on_done(chosen)

    C.form_dialog(S.page, "Assign to", [ft.Column(boxes, spacing=0) if boxes else ft.Text("No one in your team yet.")],
                  action(submit), "Assign")


async def path_editor_view(S: AppState, q: dict, path_id: str) -> ft.Control:
    S.ctx.require_author()
    pid = uuid.UUID(path_id)
    p = await S.run(paths.get_path, S.ctx, pid)
    draft_id = await S.run(paths.ensure_draft, S.ctx, pid)
    steps = await S.run(paths.version_steps, S.ctx, draft_id)
    library = await S.run(content.list_items, S.ctx)
    stages = list(dict.fromkeys(s.stage for s in steps))

    @action
    async def publish(e):
        v = await S.run(paths.publish, S.ctx, pid)
        S.toast(f"Published version {v}. People already enrolled stay on their version until you update them.")
        await S.refresh()

    @action
    async def move(e):
        sid, delta = e.control.data
        await S.run(paths.move_step, S.ctx, pid, sid, delta)
        await S.refresh()

    def delete(e):
        sid = e.control.data

        async def yes():
            await S.run(paths.delete_step, S.ctx, pid, sid)
            await S.refresh()

        C.confirm(S.page, "Delete this step?", "It's removed from the draft. Published versions aren't affected.",
                  action(yes), "Delete", danger=True)

    @action
    async def assign(e):
        people = [x for x in await S.run(orgs.list_people, S.ctx)]

        async def done(chosen):
            n = await S.run(paths.enroll, S.ctx, pid, chosen)
            S.toast(f"Assigned to {n} {'person' if n == 1 else 'people'}.")

        people_picker(S, people, done)

    rows: list[ft.Control] = []
    for stage, group in groupby(steps, key=lambda s: s.stage):
        rows.append(ft.Text(stage, theme_style=ft.TextThemeStyle.LABEL_LARGE, color=ft.Colors.ON_SURFACE_VARIANT))
        for s in group:
            rows.append(ft.ListTile(
                leading=ft.Icon(STEP_ICONS.get(s.step_type)), title=ft.Text(s.title),
                subtitle=ft.Text(f"Due day {s.due_offset_days}" + (" · sign-off" if s.requires_signoff else "")
                                 + (f" · {s.content_title}" if s.content_title else "")),
                trailing=ft.Row([
                    ft.IconButton(ft.Icons.ARROW_UPWARD, tooltip="Move up", data=(s.id, -1), on_click=move),
                    ft.IconButton(ft.Icons.ARROW_DOWNWARD, tooltip="Move down", data=(s.id, 1), on_click=move),
                    ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit", data=s,
                                  on_click=lambda e: step_dialog(S, pid, library, e.control.data, stages)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", data=s.id, on_click=delete),
                ], tight=True, spacing=0)))
    status = "Draft" if not p.published_version_id else "Editing a new draft (the published version stays live)"
    return C.page_body(
        ft.TextButton("All paths", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/paths")),
        ft.Text(status, color=ft.Colors.ON_SURFACE_VARIANT),
        C.card(ft.Column([*rows] if rows else [C.empty("Add the first step.")], spacing=2)),
        ft.Row([ft.OutlinedButton("Add step", icon=ft.Icons.ADD, on_click=lambda e: step_dialog(S, pid, library, None, stages)),
                ft.FilledButton("Publish", icon=ft.Icons.PUBLISH, on_click=publish),
                ft.FilledTonalButton("Assign to people", icon=ft.Icons.PERSON_ADD_ALT, on_click=assign,
                                     disabled=not p.published_version_id)], wrap=True),
        title=p.title, subtitle=p.description)
