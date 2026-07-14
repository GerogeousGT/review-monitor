"""Постоянно работающий веб-процесс review-monitor — единственный в проекте (весь
остальной код это одноразовые батч-скрипты по расписанию, см. PLAN.md). Логин +
список подключённых клиентов + сам дашборд (в разработке) — позже сюда же добавится
webhook для Telegram inline-кнопок (см. PLAN.md, CHANGELOG 2026-07-14).

Пользователи (auth_db.py, отдельная users.db) — роль admin видит всех clients/*,
роль client привязана к одному client_slug и видит только его.
"""
import os
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

import auth_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = PROJECT_ROOT / "clients"

app = Flask(__name__)
app.secret_key = os.environ.get("WEBAPP_SECRET_KEY", "dev-only-change-me")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.role = row["role"]
        self.client_slug = row["client_slug"]
        self.must_change_password = bool(row["must_change_password"])


@login_manager.user_loader
def load_user(user_id: str):
    conn = auth_db.get_connection()
    auth_db.init_db(conn)
    row = auth_db.get_user_by_id(conn, int(user_id))
    conn.close()
    return User(row) if row else None


def _visible_clients(user: User) -> list[str]:
    """admin видит все clients/<slug>/ на диске, client — только свой."""
    if user.role == "admin":
        if not CLIENTS_DIR.is_dir():
            return []
        return sorted(p.name for p in CLIENTS_DIR.iterdir() if p.is_dir())
    return [user.client_slug] if user.client_slug else []


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "review-monitor-webapp"})


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = auth_db.get_connection()
        auth_db.init_db(conn)
        row = auth_db.get_user_by_username(conn, username)
        conn.close()

        if row and check_password_hash(row["password_hash"], password):
            user = User(row)
            login_user(user)
            if user.must_change_password:
                return redirect(url_for("change_password"))
            return redirect(url_for("index"))
        error = "Неверный логин или пароль."

    return render_template("login.html", error=error)


@app.before_request
def _require_password_change():
    """Перехватывает ЛЮБОЙ защищённый роут, пока временный пароль не сменён —
    иначе пользователь мог бы обойти смену прямой ссылкой на /."""
    if not current_user.is_authenticated:
        return None
    if not getattr(current_user, "must_change_password", False):
        return None
    if request.endpoint in ("change_password", "logout", "static", "health"):
        return None
    return redirect(url_for("change_password"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if len(new_password) < 8:
            error = "Пароль должен быть не короче 8 символов."
        elif new_password != confirm:
            error = "Пароли не совпадают."
        else:
            conn = auth_db.get_connection()
            auth_db.init_db(conn)
            auth_db.set_password(conn, int(current_user.id), generate_password_hash(new_password))
            conn.close()
            current_user.must_change_password = False
            return redirect(url_for("index"))

    return render_template("change_password.html", error=error, forced=current_user.must_change_password)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    clients = _visible_clients(current_user)
    return render_template("index.html", clients=clients, username=current_user.username)


@app.route("/dashboard/<slug>")
@login_required
def dashboard(slug: str):
    allowed = _visible_clients(current_user)
    if slug not in allowed:
        return "Доступ запрещён", 403
    # Дашборд — следующий шаг (см. PLAN.md), пока заглушка.
    return render_template("dashboard_stub.html", slug=slug)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8789)
