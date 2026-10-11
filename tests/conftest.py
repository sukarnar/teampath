import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get("TEST_DB_NAME", "teampath_test")
PG_ADMIN = os.environ.get("TEST_PG_ADMIN_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/postgres")
host = PG_ADMIN.split("@")[1].rsplit("/", 1)[0]
os.environ["DATABASE_URL"] = f"postgresql+psycopg://teampath_app:teampath_app@{host}/{TEST_DB}"
os.environ["MIGRATION_DATABASE_URL"] = f"postgresql+psycopg://postgres:postgres@{host}/{TEST_DB}"
os.environ["REQUIRE_ORG_APPROVAL"] = "false"
os.environ["DATA_DIR"] = str(ROOT / ".testdata")
os.environ["ENVIRONMENT"] = "development"


@pytest.fixture(scope="session", autouse=True)
def database():
    from sqlalchemy import create_engine, text

    eng = create_engine(PG_ADMIN, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f"drop database if exists {TEST_DB} with (force)"))
        c.execute(text(f"create database {TEST_DB}"))
    eng.dispose()
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True, env=os.environ.copy())
    from teampath.config import get_settings
    from teampath.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    yield


PASSWORD = "Correct-Horse-Battery-9"


class World:
    """Helpers to build organizations and hierarchies through the real services."""

    def org(self, name="Acme Agency", org_type="insurance"):
        from teampath.services import auth

        email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        auth.signup_organization(name="Olivia Owner", email=email, password=PASSWORD, org_name=name, org_type=org_type)
        return self.ctx(email)

    def ctx(self, email, org_id=None):
        from teampath.services import auth

        r = auth.login(email, PASSWORD)
        assert r.status == "ok", r
        orgs = auth.my_organizations(r.user.id)
        return auth.open_organization(r.user, org_id or orgs[0].org_id)

    def member(self, inviter, name, role="member", parent=None):
        from teampath.services import auth, orgs

        email = f"{name.lower().replace(' ', '.')}-{uuid.uuid4().hex[:6]}@example.com"
        raw = orgs.invite(inviter, email, role=role, parent_membership_id=parent.membership_id if parent else None)
        auth.accept_invitation(raw, name=name, password=PASSWORD)
        return self.ctx(email, inviter.org_id)


@pytest.fixture
def world():
    return World()
