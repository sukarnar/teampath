"""Content library: browse, add and update documents, slides, videos, links and text."""

from __future__ import annotations


import flet as ft

from ...services import content
from ...storage import ALLOWED
from .. import components as C
from ..state import AppState, action

KIND_ICON = {"document": ft.Icons.DESCRIPTION_OUTLINED, "slides": ft.Icons.SLIDESHOW_OUTLINED,
             "video": ft.Icons.PLAY_CIRCLE_OUTLINE, "link": ft.Icons.LINK, "text": ft.Icons.NOTES}
FILE_EXT = [e.strip(".") for e in ALLOWED if e not in (".txt", ".md")]


def add_dialog(S: AppState) -> None:
    kind = ft.SegmentedButton(selected=["file"], segments=[
        ft.Segment(value="file", label=ft.Text("File"), icon=ft.Icon(ft.Icons.UPLOAD_FILE)),
        ft.Segment(value="link", label=ft.Text("Link"), icon=ft.Icon(ft.Icons.LINK)),
        ft.Segment(value="text", label=ft.Text("Text"), icon=ft.Icon(ft.Icons.NOTES))])
    title = C.text_field("Title")
    desc = C.text_field("Description (optional)", multiline=True, min_lines=1, max_lines=4)
    tags = C.text_field("Tags (comma separated)", hint_text="e.g. licensing, scripts, compliance")
    url = C.text_field("https:// link", visible=False)
    body = C.text_field("Text (Markdown supported)", multiline=True, min_lines=6, max_lines=16, visible=False)
    picked: dict = {}
    file_label = ft.Text("No file chosen", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    @action
    async def choose(e):
        res = await S.pick_and_upload(FILE_EXT, "Choose a document, deck, image or video")
        if res:
            picked["path"], picked["name"] = res
            file_label.value = res[1]
            if not title.value:
                title.value = res[1].rsplit(".", 1)[0].replace("_", " ")

    choose_btn = ft.OutlinedButton("Choose file", icon=ft.Icons.ATTACH_FILE, on_click=choose)
    file_row = ft.Row([choose_btn, file_label], wrap=True)
    team_only = ft.Switch(label="Only my team can see this", value=not S.ctx.is_admin, disabled=not S.ctx.is_admin)
    allow_dl = ft.Switch(label="Allow downloading the original", value=True)
    watermark = ft.Switch(label="Watermark pages with the viewer's name", value=False)

    def on_kind(e):
        k = kind.selected[0]
        file_row.visible = k == "file"
        url.visible = k == "link"
        body.visible = k == "text"
        allow_dl.visible = watermark.visible = k == "file"

    kind.on_change = on_kind

    async def submit():
        k = kind.selected[0]
        if k == "file" and not picked:
            S.toast("Choose a file to upload.", error=True)
            return False
        iid = await S.run(content.create_item, S.ctx, title=title.value, kind="document" if k == "file" else k,
                          description=desc.value, tags=tags.value.split(","), url=url.value, body_md=body.value,
                          tmp_path=picked.get("path"), original_name=picked.get("name"), team_only=team_only.value,
                          allow_download=allow_dl.value, watermark=watermark.value)
        S.toast("Added to the library.")
        S.go(f"/content/{iid}")

    C.form_dialog(S.page, "Add to the library", [kind, title, file_row, url, body, desc, tags, team_only, allow_dl,
                                                  watermark], action(submit), "Add", width=560)


def edit_dialog(S: AppState, item: content.ContentView) -> None:
    title = C.text_field("Title", value=item.title)
    desc = C.text_field("Description", value=item.description or "", multiline=True, min_lines=1, max_lines=4)
    tags = C.text_field("Tags (comma separated)", value=", ".join(item.tags))
    allow_dl = ft.Switch(label="Allow downloading the original", value=item.allow_download)
    watermark = ft.Switch(label="Watermark pages with the viewer's name", value=item.watermark)
    archive = ft.Switch(label="Archive (hide from the library)", value=False)

    @action
    async def new_version(e):
        if item.kind in ("document", "slides", "video"):
            res = await S.pick_and_upload(FILE_EXT, "Choose the new version")
            if not res:
                return
            await S.run(content.add_version, S.ctx, item.id, tmp_path=res[0], original_name=res[1])
            S.toast("New version uploaded. Steps that use this material now show it.")

    async def submit():
        await S.run(content.update_item, S.ctx, item.id, title=title.value, description=desc.value,
                    tags=tags.value.split(","), allow_download=allow_dl.value, watermark=watermark.value,
                    archived=archive.value)
        S.toast("Saved.")
        S.go("/library")

    fields = [title, desc, tags]
    if item.kind in ("document", "slides", "video"):
        fields += [allow_dl, watermark, ft.Row([ft.OutlinedButton("Upload new version", icon=ft.Icons.UPLOAD,
                                                                  on_click=new_version)])]
    fields.append(archive)
    C.form_dialog(S.page, f"Edit: {item.title}", fields, action(submit), width=520)


async def library_view(S: AppState, q: dict) -> ft.Control:
    search = q.get("q", "")
    items = await S.run(content.list_items, S.ctx, search)
    box = C.text_field("Search the library", value=search, prefix_icon=ft.Icons.SEARCH,
                       on_submit=lambda e: S.go(f"/library?q={e.control.value}"))
    cards = []
    for it in items:
        sub = [it.kind.title()]
        if it.version_no and it.version_no > 1:
            sub.append(f"v{it.version_no}")
        if it.preview_status == "pending":
            sub.append("preparing preview")
        elif it.preview_status == "ready" and it.kind in ("document", "slides"):
            sub.append("preview ready")
        cards.append(ft.Container(C.card(ft.Column([
            ft.Row([ft.Icon(KIND_ICON.get(it.kind), color=ft.Colors.PRIMARY),
                    ft.Text(it.title, weight=ft.FontWeight.W_600, expand=True, max_lines=2),
                    *([ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit", data=it,
                                     on_click=lambda e: edit_dialog(S, e.control.data))] if it.can_edit else [])]),
            ft.Text(it.description or "", max_lines=2, size=13, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([*([C.pill("Team only", icon=ft.Icons.GROUP_OUTLINED)] if it.scoped else []),
                    *[C.pill(t) for t in it.tags[:3]]], wrap=True, spacing=4),
            ft.Text(" · ".join(sub), size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=6), on_click=lambda e, i=it.id: S.go(f"/content/{i}")), col={"xs": 12, "sm": 6, "lg": 4}))
    if q.get("edit") and S.ctx.is_author:
        target = next((i for i in items if str(i.id) == q["edit"] and i.can_edit), None)
        if target:
            S.page.run_task(action(lambda: _later(lambda: edit_dialog(S, target))))
    actions = [ft.FilledButton("Add", icon=ft.Icons.ADD, on_click=lambda e: add_dialog(S))] if S.ctx.is_author else []
    return C.page_body(box, ft.ResponsiveRow(cards, spacing=12, run_spacing=12) if cards else
                       C.empty("Nothing here yet." if not search else "No matches."),
                       title="Library", subtitle="Documents, decks, videos, links and guides", actions=actions)


async def _later(fn):
    import asyncio

    await asyncio.sleep(0.2)
    fn()
