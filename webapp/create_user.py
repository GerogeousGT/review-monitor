"""Создать пользователя дашборда — вручную через CLI, без UI регистрации (пока
единственный сценарий — Жорж заводит себя как admin и позже клиентов вручную).

Пароль ВСЕГДА запрашивается через getpass (не argv) — позиционный пароль в
командной строке попадает в shell history и вывод `ps`, для single-admin
сценария это не оправданный риск (исправлено 2026-07-17).

Использование:
  python create_user.py admin admin
  python create_user.py worldclass_owner client worldclass
"""
import sys
from getpass import getpass

from werkzeug.security import generate_password_hash

import auth_db


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    username = sys.argv[1]
    password = getpass("Пароль: ")
    role = sys.argv[2] if len(sys.argv) > 2 else "admin"
    client_slug = sys.argv[3] if len(sys.argv) > 3 else None

    if role == "client" and not client_slug:
        print("Для роли client нужен client_slug четвёртым аргументом.")
        return

    conn = auth_db.get_connection()
    auth_db.init_db(conn)

    if auth_db.get_user_by_username(conn, username):
        print(f"Пользователь '{username}' уже существует.")
        return

    password_hash = generate_password_hash(password)
    user_id = auth_db.create_user(conn, username, password_hash, role, client_slug)
    print(f"Создан пользователь #{user_id} '{username}' (роль: {role}"
          + (f", клиент: {client_slug}" if client_slug else "") + ")")
    conn.close()


if __name__ == "__main__":
    main()
