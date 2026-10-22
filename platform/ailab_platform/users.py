"""Manage accounts from the command line.

    python -m ailab_platform.users add alice            # asks for the password (or --password-stdin)
    python -m ailab_platform.users passwd alice         # also ends alice's sessions
    python -m ailab_platform.users disable alice        # cannot log in, sessions stop working
    python -m ailab_platform.users enable alice
    python -m ailab_platform.users list

On the k3s deployment:  kubectl -n platform exec -it deploy/platform-api -- python -m ailab_platform.users add alice
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from .accounts import AccountError, create_user, normalize_username, revoke_sessions, set_password
from .db import SessionLocal, init_db
from .models import User, utcnow


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat: ") != first:
        raise AccountError("the two passwords differ")
    return first


def _find(db, username: str) -> User:
    user = db.scalar(select(User).where(User.username == normalize_username(username)))
    if user is None:
        raise AccountError(f"no such user: {username}")
    return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ailab_platform.users", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "passwd"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
        if name == "add":
            p.add_argument("--name", default="", help="display name (default: the username)")
            p.add_argument("--email", default="", help="default: <username>@ailab.local")
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    init_db()  # migrate first: this may be the first process to touch a fresh database
    with SessionLocal() as db:
        try:
            if args.command == "add":
                user = create_user(db, args.username, _read_password(args), display_name=args.name, email=args.email)
                print(f"created {user.username} ({user.id})")
            elif args.command == "passwd":
                user = _find(db, args.username)
                set_password(db, user, _read_password(args))
                print(f"password changed for {user.username}; their sessions were ended")
            elif args.command in ("disable", "enable"):
                user = _find(db, args.username)
                user.disabled_at = utcnow() if args.command == "disable" else None
                if args.command == "disable":
                    revoke_sessions(db, user.id)
                print(f"{args.command}d {user.username}")
            else:
                for user in db.scalars(select(User).where(User.username.is_not(None)).order_by(User.created_at)):
                    print(f"{user.username:<24}{'disabled' if user.disabled_at else 'active':<10}{user.email}")
            db.commit()
        except AccountError as exc:
            db.rollback()
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
