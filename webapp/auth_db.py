"""Отдельная БД под логины/пароли — не смешивается с clients/<slug>/db/reviews.db
(та база про отзывы конкретного клиента, эта — про то, кто вообще имеет доступ
к дашборду и к каким клиентам). Одна SQLite на весь webapp, не per-client.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "users.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'client',  -- admin | client
    client_slug TEXT,                      -- NULL для admin (видит всех clients/*), иначе конкретный slug
    must_change_password INTEGER NOT NULL DEFAULT 0
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "must_change_password" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def create_user(
    conn: sqlite3.Connection, username: str, password_hash: str, role: str, client_slug: str | None,
    must_change_password: bool = True,
) -> int:
    """must_change_password=True по умолчанию — пароль обычно выдаёт Жорж вручную
    (сгенерированный или сказанный по телефону), пользователь должен сменить его
    на свой при первом входе, а не продолжать пользоваться чужим временным паролем."""
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, client_slug, must_change_password) VALUES (?, ?, ?, ?, ?)",
        (username, password_hash, role, client_slug, int(must_change_password)),
    )
    conn.commit()
    return cur.lastrowid


def set_password(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
        (password_hash, user_id),
    )
    conn.commit()
