import shutil
import time
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from teampath.db import system_session
from teampath.models import Thread
from teampath.services import content, notify, qa
from teampath.worker import tasks
from teampath.worker.main import drain


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")
def test_office_document_preview(world, tmp_path):
    owner = world.org()
    src = tmp_path / "guide.txt"
    src.write_text("Carrier appointment guide\n\nStep one: complete the contracting packet.\n")
    subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp_path}/p", "--headless", "--convert-to", "docx",
                    "--outdir", str(tmp_path), str(src)], check=True, capture_output=True, timeout=120)
    docx = tmp_path / "guide.docx"
    upload = tmp_path / "upload.docx"
    shutil.copy(docx, upload)
    iid = content.create_item(owner, title="Guide", kind="document", tmp_path=upload, original_name="guide.docx")
    drain()
    item = content.open_item(owner, iid)
    assert item.preview_status == "ready" and len(item.page_urls) >= 1
    assert item.download_url


def test_rejects_disguised_files(world, tmp_path):
    from teampath.context import ValidationError

    owner = world.org()
    fake = tmp_path / "evil.pdf"
    fake.write_bytes(b"MZ\x90\x00 not a pdf")
    with pytest.raises(ValidationError):
        content.create_item(owner, title="x", kind="document", tmp_path=fake, original_name="evil.pdf")
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ")
    with pytest.raises(ValidationError):
        content.create_item(owner, title="x", kind="document", tmp_path=exe, original_name="tool.exe")


def test_question_escalates_up_the_hierarchy(world):
    owner = world.org()
    leader = world.member(owner, "Lee", role="leader")
    agent = world.member(leader, "Ann", parent=leader)
    tid = qa.ask(agent, "Anyone?", "Need help")
    with system_session("test") as db:
        db.execute(update(Thread).where(Thread.id == tid).values(
            last_activity_at=datetime.now(timezone.utc) - timedelta(hours=30)))
    tasks.escalate_questions()
    t = qa.get_thread(owner, tid)
    assert t.assigned_to_name == "Olivia Owner"
    assert any(n.kind == "question_escalated" for n in notify.my_notifications(owner))


def test_periodic_momentum_refresh(world):
    owner = world.org()
    world.member(owner, "Ann")
    tasks.refresh_all_momentum()


def test_worker_events_reach_the_web_app_through_postgres():
    """The worker runs in another container; its events travel via LISTEN/NOTIFY."""
    import threading

    from teampath import events

    got = threading.Event()
    events.subscribe("org:test-bridge", 991, lambda e: got.set() if e.get("kind") == "content" else None)
    events.start_listener()
    time.sleep(1.0)  # listener connects
    try:
        events.use_postgres_notify()
        events.publish("org:test-bridge", kind="content", version_id="x")
    finally:
        events._mode = "local"
    assert got.wait(5), "event did not arrive through Postgres"
    events.unsubscribe_all(991)


def test_office_preview_waits_for_worker_but_pdf_does_not(world, tmp_path):
    import fitz

    owner = world.org()
    pdf = tmp_path / "a.pdf"
    doc = fitz.open()
    doc.new_page()
    pdf.write_bytes(doc.tobytes())
    iid = content.create_item(owner, title="PDF", kind="document", tmp_path=pdf, original_name="a.pdf")
    v = content.open_item(owner, iid)
    assert v.preview_status == "ready" and v.page_count == 1 and v.download_url

    locked = tmp_path / "locked.pdf"
    doc = fitz.open()
    doc.new_page()
    locked.write_bytes(doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u"))
    iid = content.create_item(owner, title="Locked", kind="document", tmp_path=locked, original_name="locked.pdf")
    v = content.open_item(owner, iid)
    assert v.preview_status == "failed" and v.download_url  # still downloadable
