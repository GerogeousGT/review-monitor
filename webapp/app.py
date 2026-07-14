"""Постоянно работающий веб-процесс review-monitor — единственный в проекте (весь
остальной код это одноразовые батч-скрипты по расписанию, см. PLAN.md). Логин +
список подключённых клиентов + дашборд (v1, см. PLAN.md "Дашборд клиента v1/v2") —
позже сюда же добавится webhook для Telegram inline-кнопок (CHANGELOG 2026-07-14).

Пользователи (auth_db.py, отдельная users.db) — роль admin видит всех clients/*,
роль client привязана к одному client_slug и видит только его.

Важно: webapp обслуживает НЕСКОЛЬКО клиентов в одном процессе одновременно — в отличие
от main_*.py батч-скриптов (там CLIENT_SLUG задаётся один раз через env на весь запуск),
здесь путь к БД клиента вычисляется явно на каждый запрос (_client_db_path), полагаться
на core.env.get_client_root()/CLIENT_SLUG нельзя.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
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

sys.path.insert(0, str(PROJECT_ROOT))
from core import db as core_db  # noqa: E402 — после sys.path.insert, так и задумано

app = Flask(__name__)
app.secret_key = os.environ.get("WEBAPP_SECRET_KEY", "dev-only-change-me")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

PLATFORM_LABELS = {"yandex_maps": "Яндекс.Карты", "zoon": "Zoon", "2gis": "2ГИС"}


@app.context_processor
def _inject_platform_labels():
    return {"platform_labels": PLATFORM_LABELS}


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


def _client_db_path(slug: str) -> Path:
    return CLIENTS_DIR / slug / "db" / "reviews.db"


def _client_display_name(slug: str) -> str:
    """Человекочитаемое имя клиента из client_config.yaml — если конфиг не найден
    или не парсится, используем slug как есть (не должно ронять страницу)."""
    try:
        import yaml
        config_path = CLIENTS_DIR / slug / "client_config.yaml"
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg["client"]["name"]
    except Exception:
        return slug


def _primary_location_id(conn) -> str | None:
    """v1: берём первую (обычно единственную) локацию клиента. Оба текущих клиента
    однолокационные — полноценный выбор между несколькими точками см. PLAN.md v2
    (heatmap филиалы×темы), непроверяемо без реального мультиточечного клиента."""
    locations = core_db.get_locations(conn)
    return locations[0] if locations else None


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


def _require_client_access(slug: str) -> Path | None:
    """Общая проверка прав + существования БД для обоих роутов дашборда. Возвращает
    путь к БД, если доступ разрешён, иначе None (вызывающая сторона решает, что вернуть)."""
    allowed = _visible_clients(current_user)
    if slug not in allowed:
        return None
    db_path = _client_db_path(slug)
    if not db_path.exists():
        return None
    return db_path


@app.route("/dashboard/<slug>")
@login_required
def dashboard(slug: str):
    db_path = _require_client_access(slug)
    if db_path is None:
        return "Доступ запрещён", 403

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    location_id = _primary_location_id(conn)

    if location_id is None:
        conn.close()
        return render_template("dashboard.html", slug=slug, display_name=_client_display_name(slug), no_data=True)

    now = datetime.now(timezone.utc)
    since_month = (now - timedelta(days=30)).isoformat()

    tag_alerts = [dict(a) for a in core_db.get_all_active_alerts(conn) if a["alert_type"] == "tag"]
    repeat_offender_alerts_raw = core_db.get_all_active_repeat_offender_alerts(conn)
    repeat_offender_alerts = []
    for a in repeat_offender_alerts_raw:
        _, author, platform = a["tag"].split(":", 2)
        repeat_offender_alerts.append({**dict(a), "author": author, "platform": platform})

    overdue_recent = core_db.get_overdue_reviews(conn)
    overdue_stale = core_db.get_stale_overdue_reviews(conn)

    sentiment_counts = core_db.get_review_sentiment_counts_since(conn, location_id, since_month)
    top_praised = core_db.get_top_tags_by_sentiment_since(conn, location_id, since_month, "positive")
    top_criticized = core_db.get_top_tags_by_sentiment_since(conn, location_id, since_month, "negative")
    platform_comparison = core_db.get_platform_comparison_since(conn, location_id, since_month)
    hidden_problems = core_db.get_hidden_problems(conn, location_id)

    conn.close()

    red_count = sum(1 for a in tag_alerts if a["severity"] == "red")
    yellow_count = sum(1 for a in tag_alerts if a["severity"] == "yellow")

    return render_template(
        "dashboard.html",
        slug=slug,
        display_name=_client_display_name(slug),
        no_data=False,
        red_count=red_count,
        yellow_count=yellow_count,
        overdue_recent=overdue_recent,
        overdue_stale=overdue_stale,
        tag_alerts=tag_alerts,
        repeat_offender_alerts=repeat_offender_alerts,
        sentiment_counts=sentiment_counts,
        top_praised=top_praised,
        top_criticized=top_criticized,
        platform_comparison=platform_comparison,
        hidden_problems=hidden_problems,
    )


@app.route("/dashboard/<slug>/reviews")
@login_required
def dashboard_reviews(slug: str):
    db_path = _require_client_access(slug)
    if db_path is None:
        return "Доступ запрещён", 403

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    location_id = _primary_location_id(conn)

    if location_id is None:
        conn.close()
        return render_template("dashboard_reviews.html", slug=slug, display_name=_client_display_name(slug), no_data=True)

    platform = request.args.get("platform") or None
    sentiment = request.args.get("sentiment") or None
    page = max(1, request.args.get("page", 1, type=int))
    limit = 20
    offset = (page - 1) * limit

    reviews, total = core_db.get_reviews_paginated(conn, location_id, platform=platform, sentiment=sentiment, offset=offset, limit=limit)
    for r in reviews:
        r["tags"] = core_db.get_review_tags(conn, r["id"])
        r["platform_label"] = PLATFORM_LABELS.get(r["platform"], r["platform"])

    conn.close()

    total_pages = max(1, (total + limit - 1) // limit)

    return render_template(
        "dashboard_reviews.html",
        slug=slug,
        display_name=_client_display_name(slug),
        no_data=False,
        reviews=reviews,
        total=total,
        page=page,
        total_pages=total_pages,
        platform_filter=platform or "",
        sentiment_filter=sentiment or "",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8789)
