"""Deployment-only account lifecycle commands; passwords never enter argv."""
import argparse
from contextlib import closing
from getpass import getpass
import sys

from .db import (
    connect, create_user, delete_user, get_user_by_username, init_db,
    update_user, validate_username, write_lock,
)


def read_password() -> str:
    password = getpass("新密码：")
    confirmation = getpass("再次输入新密码：")
    if password != confirmation:
        raise ValueError("两次输入的密码不一致")
    if not 8 <= len(password) <= 256:
        raise ValueError("密码须为 8–256 个字符")
    return password


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="部署端账号管理（不提供跨账号 Web 管理）")
    parser.add_argument("command", choices=("create", "reset-password", "delete"))
    parser.add_argument("username")
    args = parser.parse_args(argv)
    try:
        validate_username(args.username)
        init_db()
        with write_lock, closing(connect()) as db:
            target = get_user_by_username(db, args.username)
            if args.command == "create":
                if target is not None:
                    raise ValueError("用户名已存在")
                create_user(db, args.username, read_password())
                print(f"账号 {args.username} 已创建")
            else:
                if target is None:
                    raise ValueError("账号不存在")
                if args.command == "reset-password":
                    update_user(db, target["id"], target["username"], read_password())
                    print(f"账号 {args.username} 的密码已重置")
                else:
                    confirmation = input("请输入完整用户名以确认删除：")
                    if confirmation != target["username"]:
                        raise ValueError("删除确认不匹配")
                    delete_user(db, target["id"])
                    print(f"账号 {args.username} 已删除")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("操作已取消", file=sys.stderr)
        return 1
    except OSError:
        print("账号操作失败，请检查部署端存储状态", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
