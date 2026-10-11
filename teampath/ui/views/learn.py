"""Learner screens: my paths, a step, and the content viewer (with Q&A)."""

from __future__ import annotations

import asyncio
import uuid
from itertools import groupby

import flet as ft

from ...services import content, paths, qa
from .. import components as C
from ..state import AppState, action

STEP_ICONS = {"read": ft.Icons.MENU_BOOK_OUTLINED, "task": ft.Icons.TASK_ALT, "acknowledge": ft.Icons.VERIFIED_OUTLINED,
              "meet": ft.Icons.HANDSHAKE_OUTLINED}
STATUS_TEXT = {"todo": "To do", "pending_signoff": "Waiting for sign-off", "done": "Done"}


def step_status_icon(s: paths.StepView) -> ft.Control:
    if s.status == "done":
        return ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.PRIMARY, tooltip="Done")
    if s.status == "pending_signoff":
        return ft.Icon(ft.Icons.HOURGLASS_TOP, color=ft.Colors.TERTIARY, tooltip="Waiting for sign-off")
    if s.locked:
        return ft.Icon(ft.Icons.LOCK_OUTLINE, color=ft.Colors.OUTLINE, tooltip="Finish earlier steps first")
    if s.overdue:
        return ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.ERROR, tooltip="Overdue")
    return ft.Icon(ft.Icons.RADIO_BUTTON_UNCHECKED, color=ft.Colors.PRIMARY)


def enrollment_card(S: AppState, e: paths.EnrollmentView, clickable=True) -> ft.Control:
    rows: list[ft.Control] = []
    for stage, steps in groupby(e.steps, key=lambda s: s.stage):
        rows.append(ft.Text(stage, theme_style=ft.TextThemeStyle.LABEL_LARGE, color=ft.Colors.ON_SURFACE_VARIANT))
        for s in steps:
            rows.append(ft.ListTile(
                leading=step_status_icon(s), title=ft.Text(s.title), dense=True,
                subtitle=ft.Text(f"{STATUS_TEXT.get(s.status)} · due {C.when(s.due_on)}"
                                 + (" · needs sign-off" if s.requires_signoff and s.status == "todo" else "")),
                trailing=ft.Icon(STEP_ICONS.get(s.step_type), size=18, color=ft.Colors.OUTLINE),
                on_click=(lambda ev, sid=s.id: S.go(f"/learn/step/{sid}")) if clickable and not s.locked else None,
            ))
    head = ft.Row([
        ft.Column([ft.Text(e.title, theme_style=ft.TextThemeStyle.TITLE_MEDIUM, weight=ft.FontWeight.W_600),
                   ft.Text(f"Started {C.when(e.start_date)} · {e.done} of {e.total} done"
                           + (" · completed" if e.status == "completed" else ""), color=ft.Colors.ON_SURFACE_VARIANT)],
                  spacing=2, expand=True),
        ft.Stack([ft.ProgressRing(value=e.percent / 100, width=48, height=48, stroke_width=5),
                  ft.Container(ft.Text(f"{e.percent}%", size=11, weight=ft.FontWeight.W_600), width=48, height=48,
                               alignment=ft.Alignment.CENTER)]),
    ])
    return C.card(ft.Column([head, ft.Divider(), *rows], spacing=4))


async def learn_view(S: AppState, q: dict) -> ft.Control:
    enrollments = await S.run(paths.my_enrollments, S.ctx)
    active = [e for e in enrollments if e.status == "active"]
    done = [e for e in enrollments if e.status == "completed"]
    blocks = [enrollment_card(S, e) for e in active]
    if not enrollments:
        blocks.append(C.empty("No onboarding paths assigned yet. Your leader will assign one.", ft.Icons.SCHOOL_OUTLINED))
    if done:
        blocks.append(C.section("Completed", *[enrollment_card(S, e) for e in done]))
    blocks.append(ft.Row([ft.OutlinedButton("Browse the library", icon=ft.Icons.FOLDER_OUTLINED,
                                            on_click=lambda e: S.go("/library"))]))
    return C.page_body(*blocks, title="Learn", subtitle="Your onboarding paths and steps")


def qa_panel(S: AppState, target_type: str, target_id: uuid.UUID | None, threads: list[qa.ThreadView],
             faq: list) -> ft.Control:
    title = C.text_field("Question (short)")
    body = C.text_field("Details", multiline=True, min_lines=2, max_lines=6)

    @action
    async def ask(e):
        tid = await S.run(qa.ask, S.ctx, title.value, body.value, target_type, target_id)
        S.toast("Question sent to your leader.")
        S.go(f"/questions/{tid}")

    faq_rows = [ft.ExpansionTile(title=ft.Text(f.question, weight=ft.FontWeight.W_500),
                                 controls=[ft.Container(C.split(f.answer), padding=ft.Padding.only(left=16, bottom=8))])
                for f in faq]
    thread_rows = [ft.ListTile(
        leading=ft.Icon(ft.Icons.QUESTION_ANSWER_OUTLINED), title=ft.Text(t.title),
        subtitle=ft.Text(f"{t.author_name} · {t.status} · {C.when(t.last_activity_at)}"),
        on_click=lambda e, tid=t.id: S.go(f"/questions/{tid}")) for t in threads]
    return ft.Column([
        *([C.section("Frequently asked", *faq_rows)] if faq_rows else []),
        C.section("Questions", *thread_rows, ft.Text("Stuck? Ask — it goes to your leader.", size=12,
                                                      color=ft.Colors.ON_SURFACE_VARIANT), title, body,
                  ft.Row([ft.FilledTonalButton("Ask a question", icon=ft.Icons.SEND, on_click=ask)])),
    ], spacing=16)


async def step_view(S: AppState, q: dict, step_id: str) -> ft.Control:
    sid = uuid.UUID(step_id)
    step, enrollment = await S.run(paths.get_step, S.ctx, sid)
    threads = await S.run(qa.threads_for, S.ctx, "step", sid)
    faq = await S.run(qa.faq_for, S.ctx, "step", sid)
    blocks: list[ft.Control] = []

    meta = [C.pill(step.stage), C.pill(step.step_type.title(), icon=STEP_ICONS.get(step.step_type))]
    if enrollment:
        meta.append(C.pill(STATUS_TEXT.get(step.status, step.status)))
        if step.due_on:
            meta.append(C.pill(("Overdue · " if step.overdue else "Due ") + C.when(step.due_on),
                               bgcolor=ft.Colors.ERROR_CONTAINER if step.overdue else ft.Colors.SECONDARY_CONTAINER,
                               color=ft.Colors.ON_ERROR_CONTAINER if step.overdue else ft.Colors.ON_SECONDARY_CONTAINER))
    blocks.append(ft.Row(meta, wrap=True, spacing=6))
    if step.instructions_md:
        blocks.append(C.card(C.split(step.instructions_md)))
    if step.content_item_id:
        blocks.append(C.card(ft.ListTile(leading=ft.Icon(ft.Icons.DESCRIPTION_OUTLINED),
                                         title=ft.Text(step.content_title or "Open material"),
                                         subtitle=ft.Text("Material for this step"),
                                         trailing=ft.Icon(ft.Icons.OPEN_IN_NEW),
                                         on_click=lambda e: S.go(f"/content/{step.content_item_id}")), padding=4))

    if enrollment and step.status == "todo" and not step.locked:
        note = C.text_field("Note for your leader (optional)", multiline=True, min_lines=1, max_lines=4)
        label = {"acknowledge": "I've read and understood this", "meet": "We met",
                 "read": "Mark as read"}.get(step.step_type, "Mark complete")

        @action
        async def complete(e):
            status = await S.run(paths.complete_step, S.ctx, sid, note.value)
            S.toast("Sent for sign-off." if status == "pending_signoff" else "Nice work — step complete.")
            S.go("/learn")

        blocks.append(C.card(ft.Column([
            note, ft.Row([ft.FilledButton(label, icon=ft.Icons.CHECK, on_click=complete)]),
            *([ft.Text("Your leader will review and sign this off.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
              if step.requires_signoff else []),
        ], spacing=10)))
    elif enrollment and step.locked:
        blocks.append(ft.Text("Finish the earlier steps in this path first.", color=ft.Colors.ON_SURFACE_VARIANT))
    elif enrollment and step.note:
        blocks.append(ft.Text(f"Your note: {step.note}", italic=True))

    blocks.append(C.card(qa_panel(S, "step", sid, threads, faq)))
    back = ft.TextButton("Back to paths", icon=ft.Icons.ARROW_BACK, on_click=lambda e: S.go("/learn"))
    return C.page_body(back, *blocks, title=step.title,
                       subtitle=enrollment.title if enrollment else None)


async def content_view(S: AppState, q: dict, item_id: str) -> ft.Control:
    iid = uuid.UUID(item_id)
    item = await S.run(content.open_item, S.ctx, iid)
    threads = await S.run(qa.threads_for, S.ctx, "content", iid)
    faq = await S.run(qa.faq_for, S.ctx, "content", iid)
    blocks: list[ft.Control] = []
    tags = [C.pill(t) for t in item.tags]
    flags = []
    if item.scoped:
        flags.append(C.pill("Team only", icon=ft.Icons.GROUP_OUTLINED))
    if item.watermark:
        flags.append(C.pill("Watermarked", icon=ft.Icons.WATER_DROP_OUTLINED))
    if not item.allow_download:
        flags.append(C.pill("View only", icon=ft.Icons.VISIBILITY_OUTLINED))
    if tags or flags:
        blocks.append(ft.Row([*flags, *tags], wrap=True, spacing=6))
    if item.description:
        blocks.append(ft.Text(item.description))

    async def open_url(url):
        await ft.UrlLauncher().launch_url(url)

    if item.kind == "text" and item.body_md:
        blocks.append(C.card(C.split(item.body_md)))
    elif item.kind == "link" and item.link_url:
        blocks.append(C.card(ft.ListTile(leading=ft.Icon(ft.Icons.LINK), title=ft.Text(item.link_url),
                                         trailing=ft.Icon(ft.Icons.OPEN_IN_NEW),
                                         on_click=action(lambda e: open_url(item.link_url))), padding=4))
    elif item.preview_status in ("pending", "scanning"):
        what = ("Checking the file for viruses…" if item.preview_status == "scanning"
                else "Converting the document for preview… this usually takes 10–30 seconds.")
        blocks.append(C.card(ft.Row([ft.ProgressRing(width=20, height=20, stroke_width=2),
                                     ft.Text(what + " This page updates by itself.", expand=True)])))
        route = f"/content/{item_id}"

        async def live(event):
            if S.page.route == route:
                await S.refresh()

        S.live_handlers["content"] = live

        async def poll():
            # Fallback in case a live update is missed: check every few seconds for up to 5 minutes.
            for _ in range(100):
                await asyncio.sleep(3)
                if S.page.route != route:
                    return
                status = await asyncio.to_thread(content.preview_state, S.ctx, iid)
                if status not in ("pending", "scanning"):
                    await S.refresh()
                    return

        S.page.run_task(poll)
    elif item.preview_status in ("failed", "blocked"):
        blocks.append(C.card(ft.Text(
            "A preview isn't available for this file (it may be password-protected or damaged). "
            "You can still open or download it." if item.preview_status == "failed"
            else "This file was blocked by the security scan.")))

    file_url = S.settings.base_url + item.download_url if item.download_url else None
    buttons = []
    if item.is_pdf and file_url:
        buttons.append(ft.FilledTonalButton("Open PDF", icon=ft.Icons.OPEN_IN_NEW,
                                            on_click=action(lambda e: open_url(file_url))))
    if item.kind == "video" and file_url:
        buttons.append(ft.FilledTonalButton("Play video", icon=ft.Icons.PLAY_CIRCLE_OUTLINE,
                                            on_click=action(lambda e: open_url(file_url))))
    elif file_url and not item.is_pdf:
        buttons.append(ft.OutlinedButton(f"Download {item.file_name or 'file'}", icon=ft.Icons.DOWNLOAD,
                                         on_click=action(lambda e: open_url(file_url))))
    if buttons:
        blocks.append(ft.Row(buttons, wrap=True))

    if item.page_urls:
        def page_box(u):
            return ft.Container(ft.Image(src=u, fit=ft.BoxFit.FIT_WIDTH, border_radius=8,
                                         error_content=ft.Text("Page couldn't load — refresh the page.")),
                                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8)

        pages = ft.Column([page_box(u) for u in item.page_urls], spacing=12)
        shown = {"n": len(item.page_urls)}
        more = ft.OutlinedButton(visible=item.page_count > shown["n"])

        def label():
            more.content = ft.Text(f"Show more pages ({shown['n']} of {item.page_count})")

        @action
        async def load_more(e):
            urls = await S.run(content.page_urls, S.ctx, iid, shown["n"] + 1, 10)
            pages.controls += [page_box(u) for u in urls]
            shown["n"] += len(urls)
            more.visible = item.page_count > shown["n"]
            label()

        label()
        more.on_click = load_more
        if item.page_count > 1:
            blocks.append(ft.Text(f"{item.page_count} pages", color=ft.Colors.ON_SURFACE_VARIANT))
        blocks += [pages, ft.Row([more], alignment=ft.MainAxisAlignment.CENTER)]
    blocks.append(C.card(qa_panel(S, "content", iid, threads, faq)))
    actions = [ft.OutlinedButton("Edit", icon=ft.Icons.EDIT_OUTLINED, on_click=lambda e: S.go(f"/library?edit={iid}"))] \
        if item.can_edit else []
    return C.page_body(*blocks, title=item.title,
                       subtitle=f"{item.kind.title()}" + (f" · version {item.version_no}" if item.version_no else ""),
                       actions=actions)
