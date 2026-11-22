"""Owner-account management, run on the server (never exposed over the web).

    docker compose exec ovh-sniper python -m app.cli create-admin      # first-time setup (only when no account exists)
    docker compose exec ovh-sniper python -m app.cli reset-password    # forgot the password (logs out all devices)
    docker compose exec ovh-sniper python -m app.cli list-users
    docker compose exec ovh-sniper python -m app.cli has-owner         # exit 0 if an account exists (for scripts)

Passwords are read with getpass (not echoed, not in shell history) or from stdin with --password-stdin.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from .i18n import T, tr


def _drop_root() -> None:
    """`docker compose exec` runs as root; act as the owner of the data dir so new SQLite/WAL files stay app-owned."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    data = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    try:
        st = os.stat(data)
    except OSError:
        return
    if st.st_uid != 0:
        os.setgid(st.st_gid)
        os.setuid(st.st_uid)


def _read_password(args, username: str) -> str:
    from .security import password_problem
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        problem = password_problem(pw, username)
        if problem:
            sys.exit(T("错误：{0}", problem))
        return pw
    if not sys.stdin.isatty():
        sys.exit(T("错误：需要交互式终端输入密码（docker compose exec 不要加 -T），或使用 --password-stdin"))
    for _ in range(3):
        pw = getpass.getpass(T("密码（至少 10 位，含字母/数字/符号中的两类，输入不显示）: "))
        problem = password_problem(pw, username)
        if problem:
            print(f"  ✗ {problem}")
            continue
        if getpass.getpass(T("再输入一次: ")) != pw:
            print(T("  ✗ 两次输入不一致"))
            continue
        return pw
    sys.exit(T("错误：尝试次数过多"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli", description=T("StockHawk 账号管理（只能在服务器上运行）"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("create-admin", T("创建所有者账号（仅当还没有任何账号时）")),
                        ("reset-password", T("重置密码并让所有已登录设备失效"))):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--username", "-u")
        p.add_argument("--password-stdin", action="store_true", help=T("从标准输入读取一行作为密码"))
    sub.add_parser("list-users", help=T("列出账号"))
    sub.add_parser("has-owner", help=T("已有账号则退出码 0，否则 1"))
    args = ap.parse_args(argv)

    _drop_root()
    import re
    from sqlalchemy import func, select
    from .db import SessionLocal, auto_migrate
    from .models import AuditLog, User
    from .security import USERNAME_RE, hash_password
    auto_migrate()

    with SessionLocal() as db:
        n = db.scalar(select(func.count(User.id))) or 0
        if args.cmd == "has-owner":
            return 0 if n else 1
        if args.cmd == "list-users":
            for u in db.scalars(select(User).order_by(User.id)):
                print(f"{u.id:>3}  {u.username:<24} {T('管理员') if u.is_admin else T('用户'):<4} {T('正常') if u.is_active else T('已停用')}")
            if not n:
                print(T("（还没有账号，运行 create-admin 创建）"))
            return 0
        if args.cmd == "create-admin":
            if n:
                print(T("已存在账号，本应用只有一个所有者。忘记密码请用 reset-password。"), file=sys.stderr)
                return 2
            name = args.username or input(T("用户名（3-32 位字母、数字、_ . -）: ")).strip()
            if not re.fullmatch(USERNAME_RE, name or ""):
                sys.exit(T("错误：用户名需 3-32 位字母、数字、下划线、点或横线"))
            pw = _read_password(args, name)
            db.add(User(username=name, password_hash=hash_password(pw), is_admin=True, is_active=True))
            db.commit()
            u = db.scalar(select(User).where(User.username == name))
            db.add(AuditLog(user_id=u.id, ip="cli", action="owner_created", detail=f"username={name}"))
            db.commit()
            print(T("✓ 已创建所有者账号 {0}，现在可以用浏览器登录。", name))
            return 0
        if args.cmd == "reset-password":
            users = list(db.scalars(select(User).order_by(User.id)))
            if not users:
                sys.exit(T("还没有账号，请先运行 create-admin"))
            name = args.username or (users[0].username if len(users) == 1 else input(T("要重置的用户名: ")).strip())
            u = next((x for x in users if x.username == name), None)
            if not u:
                sys.exit(T("错误：没有用户 {0}", name))
            pw = _read_password(args, u.username)
            u.password_hash = hash_password(pw)
            u.session_epoch = (u.session_epoch or 0) + 1
            u.is_active = True
            db.add(AuditLog(user_id=u.id, ip="cli", action="password_reset", detail="via CLI"))
            db.commit()
            print(T("✓ 已重置 {0} 的密码，所有已登录设备需要重新登录。", u.username))
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
