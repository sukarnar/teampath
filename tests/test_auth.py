import pyotp
import pytest

from teampath import mailer
from teampath.context import PermissionDenied, ValidationError
from teampath.security import decrypt, sign_file_url, verify_file_signature
from teampath.services import auth, orgs
from conftest import PASSWORD


def test_lockout_after_failed_logins(world):
    owner = world.org()
    for _ in range(5):
        assert auth.login(owner.user_email, "wrong-password-123").status == "invalid"
    assert auth.login(owner.user_email, PASSWORD).status == "locked"


def test_unknown_email_looks_like_wrong_password(world):
    assert auth.login("nobody@example.com", "whatever-12345").status == "invalid"


def test_weak_password_rejected():
    with pytest.raises(ValidationError):
        auth.signup_organization(name="X", email="x@example.com", password="short", org_name="Org X")
    with pytest.raises(ValidationError):
        auth.signup_organization(name="X", email="x@example.com", password="password123", org_name="Org X")


def test_mfa_enrolment_and_login(world):
    owner = world.org()
    user = auth.login(owner.user_email, PASSWORD).user
    secret, uri = auth.begin_mfa_setup(user)
    assert not auth.confirm_mfa_setup(user.id, secret, "000000")
    assert auth.confirm_mfa_setup(user.id, secret, pyotp.TOTP(secret).now())
    r = auth.login(owner.user_email, PASSWORD)
    assert r.status == "mfa_required"
    assert auth.verify_login_mfa(r.user.id, "123456").status == "invalid"
    assert auth.verify_login_mfa(r.user.id, pyotp.TOTP(secret).now()).status == "ok"


def test_session_tokens_and_password_change_revokes_others(world):
    owner = world.org()
    uid = owner.user_id
    t1 = auth.create_session_token(uid, True, "laptop", None)
    t2 = auth.create_session_token(uid, True, "phone", None)
    assert auth.resume_session(t1) and auth.resume_session(t2)
    auth.change_password(uid, PASSWORD, "A-New-Strong-Password-42", keep_token=t1)
    assert auth.resume_session(t1) is not None
    assert auth.resume_session(t2) is None
    auth.revoke_session_token(t1)
    assert auth.resume_session(t1) is None


def test_password_reset_flow(world):
    owner = world.org()
    mailer.OUTBOX.clear()
    auth.request_password_reset(owner.user_email)
    token = mailer.OUTBOX[-1]["body"].split("token=")[1].split()[0]
    auth.reset_password(token, "Brand-New-Password-77")
    assert auth.login(owner.user_email, "Brand-New-Password-77").status == "ok"
    with pytest.raises(ValidationError):
        auth.reset_password(token, "Another-Password-88")  # single use


def test_invitations_rules(world):
    owner = world.org()
    leader = world.member(owner, "Lee", role="leader")
    member = world.member(leader, "Ann", parent=leader)
    with pytest.raises(PermissionDenied):
        orgs.invite(leader, "admin@example.com", role="admin")
    with pytest.raises(PermissionDenied):
        orgs.invite(member, "x@example.com", role="leader")
    orgs.invite(member, "recruit@example.com")  # agents recruit into their own downline
    orgs.update_settings(owner, members_can_recruit=False)
    with pytest.raises(PermissionDenied):
        orgs.invite(member, "x2@example.com")
    other = world.member(owner, "Otto", role="leader")
    with pytest.raises(Exception):
        orgs.invite(leader, "y@example.com", parent_membership_id=other.membership_id)
    raw = orgs.invite(leader, "z@example.com")
    assert auth.inspect_invitation(raw).role == "member"
    auth.accept_invitation(raw, name="Zed", password=PASSWORD)
    assert auth.inspect_invitation(raw) is None  # single use


def test_signed_file_urls():
    url = sign_file_url("f1", "u1", "orig", ttl=60)
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    assert verify_file_signature("f1", "u1", "orig", int(q["e"]), q["s"])
    assert not verify_file_signature("f1", "u2", "orig", int(q["e"]), q["s"])
    assert not verify_file_signature("f1", "u1", "p1n", int(q["e"]), q["s"])


def test_mfa_secret_encrypted_at_rest(world):
    from teampath.db import system_session
    from teampath.models import User

    owner = world.org()
    user = auth.login(owner.user_email, PASSWORD).user
    secret, _ = auth.begin_mfa_setup(user)
    auth.confirm_mfa_setup(user.id, secret, pyotp.TOTP(secret).now())
    with system_session("test") as db:
        stored = db.get(User, user.id).mfa_secret_enc
    assert secret not in stored and decrypt(stored) == secret


def test_pending_org_cannot_be_opened(world, monkeypatch):
    from teampath.config import get_settings

    monkeypatch.setattr(get_settings(), "require_org_approval", True)
    auth.signup_organization(name="Pat", email="pat@example.com", password=PASSWORD, org_name="Pending Org")
    r = auth.login("pat@example.com", PASSWORD)
    org = auth.my_organizations(r.user.id)[0]
    assert org.org_status == "pending"
    with pytest.raises(PermissionDenied):
        auth.open_organization(r.user, org.org_id)
