"""Bootstrap CLI——把邮箱加入白名单并可选地创建/提升为 admin。

用法：
    uv run python -m app.scripts.bootstrap <email> \\
        [--role admin] [--display-name NAME] [--password PWD]

行为契约（幂等，重复执行不报错）：
  1. allowed_emails：若 email 不存在则 INSERT。
  2. users：
     - 不存在 → 创建（role 默认 member；--role admin 则 admin）；
       密码从 --password 或 stdin 读取（如果没给密码但要创建用户，则 password_hash=None，
       将来可用 OAuth 或 magic link 登录）。
     - 已存在：若 --role admin 则 UPDATE role='admin'（允许提权）。
  3. 输出一行总结日志，给调用脚本 grep。

注意：本脚本复用 apps/api/app/db.py 的 async session 和 apps/api/app/security.py 的
PasswordHasher 实例，保证哈希参数与线上登录一致。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from lumen_core.models import AllowedEmail, User

from ..db import SessionLocal
from ..security import hash_password


# Why: align with V1 password policy. apps/api/app/security.py 当前未导出常量；
# 选 8 与常见 OWASP/NIST 最低基线一致，后续如 security.py 引入 _PASSWORD_MIN_LEN
# 应同步改这里。
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_RETRIES = 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="app.scripts.bootstrap",
        description="Seed an allowed email / admin user for Lumen.",
    )
    parser.add_argument("email", help="Email to allowlist and optionally create user for.")
    parser.add_argument(
        "--role",
        choices=("member", "admin"),
        default="member",
        help="Role for the user (default: member).",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Display name if creating a new user (default: derived from email local part).",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Password in plain text. If omitted and a user needs to be created, "
        "you will be prompted on stdin.",
    )
    return parser.parse_args(argv)


def _default_display_name(email: str) -> str:
    local, _, _ = email.partition("@")
    return local or email


def _read_password_interactive() -> str | None:
    """Prompt for password twice; retry up to _MAX_PASSWORD_RETRIES on mismatch / weak.

    Returns the password on success, None if the operator empties the prompt
    (deliberate skip) or hits EOF/Ctrl-C. Exits with code 2 after exhausting
    retries.
    """
    for attempt in range(1, _MAX_PASSWORD_RETRIES + 1):
        try:
            first = getpass.getpass("Password (leave empty to skip): ")
        except (EOFError, KeyboardInterrupt):
            return None
        if not first:
            return None
        if len(first) < _MIN_PASSWORD_LEN:
            print(
                f"password too short (min {_MIN_PASSWORD_LEN} chars); "
                f"attempt {attempt}/{_MAX_PASSWORD_RETRIES}",
                file=sys.stderr,
            )
            continue
        try:
            second = getpass.getpass("Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            return None
        if first != second:
            print(
                f"passwords do not match; attempt {attempt}/{_MAX_PASSWORD_RETRIES}",
                file=sys.stderr,
            )
            continue
        return first

    print(
        f"failed to set password after {_MAX_PASSWORD_RETRIES} attempts; aborting",
        file=sys.stderr,
    )
    sys.exit(2)


async def _ensure_allowed_email(session, email: str) -> bool:
    """Return True if INSERT happened, False if already present."""
    existing = (
        await session.execute(
            select(AllowedEmail).where(AllowedEmail.email == email)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(AllowedEmail(email=email))
    return True


async def _upsert_user(
    session,
    *,
    email: str,
    role: str,
    display_name: str | None,
    password: str | None,
) -> tuple[str, User]:
    """Return (action, user) where action in {'created', 'promoted', 'unchanged'}."""
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    if user is None:
        pwd_hash = hash_password(password) if password else None
        user = User(
            email=email,
            email_verified=False,
            password_hash=pwd_hash,
            display_name=display_name or _default_display_name(email),
            role=role,
        )
        session.add(user)
        return "created", user

    # 已存在
    action = "unchanged"
    if role == "admin" and user.role != "admin":
        user.role = "admin"
        action = "promoted"
    return action, user


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    email = args.email.strip().lower()
    if not email or "@" not in email:
        print(f"invalid email: {args.email!r}", file=sys.stderr)
        return 2
    if args.password == "":
        print(
            "--password must not be empty; omit it to skip password setup",
            file=sys.stderr,
        )
        return 2

    # 解决密码：--password 有值就用；没给就看是否需要提示
    password: str | None = args.password
    needs_password_prompt = password is None

    async with SessionLocal() as session:
        inserted_allowed = await _ensure_allowed_email(session, email)

        # 查 user 是否存在，决定要不要提示密码
        existing_user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if existing_user is None and needs_password_prompt:
            # 需要创建新用户且命令行没给密码 → prompt
            password = _read_password_interactive()

        action, user = await _upsert_user(
            session,
            email=email,
            role=args.role,
            display_name=args.display_name,
            password=password or None,
        )

        await session.commit()

    # 总结输出
    parts: list[str] = []
    parts.append("allowed_email=" + ("inserted" if inserted_allowed else "existed"))
    if action == "created":
        parts.append(f"created {args.role} user id={user.id}")
    elif action == "promoted":
        parts.append(f"user already exists, promoted to admin id={user.id}")
    else:
        parts.append(f"user unchanged id={user.id} role={user.role}")
    print("bootstrap: " + " ; ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
