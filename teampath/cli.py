"""Admin commands.

    python -m teampath.cli create-platform-admin you@example.com "Your Name"
    python -m teampath.cli worker-once        # run queued background jobs once
"""

from __future__ import annotations

import getpass
import sys

from sqlalchemy import select

from .db import system_session
from .models import User
from .security import hash_password, password_problems


def create_platform_admin(email: str, name: str) -> None:
    email = email.strip().lower()
    with system_session("cli: create platform admin") as db:
        u = db.scalar(select(User).where(User.email == email))
        if u:
            u.is_platform_admin = True
            print(f"{email} is now a platform admin.")
            return
    while True:
        pw = getpass.getpass("Password: ")
        problems = password_problems(pw, email, name)
        if not problems and pw == getpass.getpass("Repeat: "):
            break
        print(" ".join(problems) or "Passwords don't match.")
    with system_session("cli: create platform admin") as db:
        db.add(User(email=email, name=name, password_hash=hash_password(pw), email_verified=True,
                    is_platform_admin=True))
    print(f"Created platform admin {email}. Sign in and turn on two-step verification under Security.")


def main(argv: list[str]) -> None:
    if len(argv) >= 3 and argv[1] == "create-platform-admin":
        create_platform_admin(argv[2], argv[3] if len(argv) > 3 else "Platform Admin")
    elif len(argv) >= 2 and argv[1] == "worker-once":
        from .worker.main import drain

        print(f"ran {drain()} job(s)")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
