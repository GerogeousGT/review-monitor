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
import charts
import period

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = PROJECT_ROOT / "clients"

sys.path.insert(0, str(PROJECT_ROOT))
from core import db as core_db  # noqa: E402 — после sys.path.insert, так и задумано
from core.config import load_config as core_load_config  # noqa: E402
from agents.alert_engine import recompute_all, recompute_repeat_offenders, recompute_zone_alerts  # noqa: E402
from agents.notifier import answer_callback_query, remove_message_keyboard  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("WEBAPP_SECRET_KEY", "dev-only-change-me")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

PLATFORM_LABELS = {"yandex_maps": "Яндекс.Карты", "zoon": "Zoon", "2gis": "2ГИС"}


@app.context_processor
def _inject_platform_labels():
    return {"platform_labels": PLATFORM_LABELS}


@app.context_processor
def _inject_pending_tag_count():
    """Бейдж "Словарь тегов (N)" на вкладке — виден со всех страниц дашборда
    клиента, не только когда уже открыта сама вкладка, иначе новый pending-тег
    легко пропустить (см. PLAN.md "Approval новых тегов")."""
    slug = (request.view_args or {}).get("slug")
    if not slug:
        return {}
    db_path = _client_db_path(slug)
    if not db_path.exists():
        return {}
    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    count = len(core_db.get_pending_tags(conn))
    conn.close()
    return {"pending_count": count}


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


def _client_bot_token(slug: str) -> str | None:
    """Свой Telegram-бот на каждого клиента (clients/<slug>/.env) — та же причина,
    что у _client_db_path: webapp обслуживает НЕСКОЛЬКО клиентов в одном процессе,
    нельзя полагаться на os.environ/CLIENT_SLUG (это для batch-скриптов, один
    процесс — один клиент). Читаем .env клиента точечно, не грузим его в общий
    os.environ (не хотим смешивать токены разных клиентов в одном процессе)."""
    from dotenv import dotenv_values
    env_path = CLIENTS_DIR / slug / ".env"
    if not env_path.exists():
        return None
    return dotenv_values(env_path).get("TELEGRAM_BOT_TOKEN")


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


# График динамики тональности (2026-07-17, запрос Жоржа 2026-07-14 — см. PLAN.md
# "Дашборд клиента v2"). Логика разрешения периода (пресеты + свой диапазон) —
# в отдельном period.py (та же причина, что у charts.py: чистый stdlib-модуль,
# тестируемый из корневого venv без Flask, см. period.py docstring).
PERIOD_PRESETS = period.PERIOD_PRESETS
DEFAULT_PERIOD = period.DEFAULT_PERIOD


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

    # Динамика тональности — период выбирается отдельно от фикс. 30-дневного
    # среза ниже (top_praised/platform_comparison и т.п. эту секцию НЕ трогают,
    # см. PLAN.md — Жорж просил график+статус-бар за выбранный период, не
    # переводить весь дашборд на плавающее окно). period_totals — статус-бар
    # 🟢🟡🔴 ПОД графиком за ТОТ ЖЕ период, не независимый расчёт.
    custom_since_raw = request.args.get("since", "")
    custom_until_raw = request.args.get("until", "")
    requested_period_key = request.args.get("period", DEFAULT_PERIOD)
    since_period, until_period, granularity, period_key = period.resolve_period(
        requested_period_key, custom_since_raw, custom_until_raw
    )
    sentiment_by_period = core_db.get_review_sentiment_counts_by_period(
        conn, location_id, since_period, until_period, granularity
    )
    sentiment_chart_svg = charts.render_diverging_bar_chart(sentiment_by_period)
    period_totals = {"positive": 0, "neutral": 0, "negative": 0}
    for row in sentiment_by_period:
        for key in period_totals:
            period_totals[key] += row[key]
    # Предзаполнение полей "свой диапазон" в форме: если сейчас реально custom
    # (см. effective_period_key выше) — эхо введённых значений как есть; иначе
    # (пресет активен) — производные даты текущего пресета, чтобы переключение
    # на "Свой диапазон" стартовало не с пустых полей.
    custom_since_value = custom_since_raw if period_key == "custom" else since_period[:10]
    custom_until_value = custom_until_raw if period_key == "custom" else until_period[:10]

    # Дерево категория→тег (2026-07-20, независимый период — правка тем же днём) —
    # СВОЙ выбор периода (cat_*), отдельный от периода графика тональности выше
    # (since_period/until_period). Изначально сделали общим на одном блоке табов,
    # но Жорж поправил: при разборе тегов неудобно каждый раз скроллить наверх,
    # чтобы сменить диапазон именно для дерева — секции листаются с разной
    # частотой. Каждый блок табов протаскивает ТЕКУЩИЕ значения ДРУГОГО блока в
    # свои ссылки/скрытые поля (см. dashboard.html) — переключение одного не
    # сбрасывает состояние другого.
    cat_custom_since_raw = request.args.get("cat_since", "")
    cat_custom_until_raw = request.args.get("cat_until", "")
    requested_cat_period_key = request.args.get("cat_period", DEFAULT_PERIOD)
    cat_since, cat_until, _, cat_period_key = period.resolve_period(
        requested_cat_period_key, cat_custom_since_raw, cat_custom_until_raw
    )
    cat_custom_since_value = cat_custom_since_raw if cat_period_key == "custom" else cat_since[:10]
    cat_custom_until_value = cat_custom_until_raw if cat_period_key == "custom" else cat_until[:10]
    category_tag_tree = core_db.get_tag_counts_by_category_since(conn, location_id, cat_since, cat_until)

    all_active_alerts = core_db.get_all_active_alerts(conn)
    tag_alerts = [dict(a) for a in all_active_alerts if a["alert_type"] == "tag"]
    zone_alerts = [dict(a) for a in all_active_alerts if a["alert_type"] == "zone"]
    repeat_offender_alerts_raw = core_db.get_all_active_repeat_offender_alerts(conn)
    repeat_offender_alerts = []
    for a in repeat_offender_alerts_raw:
        _, author, platform = a["tag"].split(":", 2)
        repeat_offender_alerts.append({**dict(a), "author": author, "platform": platform})

    overdue_recent = core_db.get_overdue_reviews(conn)
    overdue_stale = core_db.get_stale_overdue_reviews(conn)

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
        zone_alerts=zone_alerts,
        repeat_offender_alerts=repeat_offender_alerts,
        sentiment_chart_svg=sentiment_chart_svg,
        period_totals=period_totals,
        period_key=period_key,
        period_presets=PERIOD_PRESETS,
        custom_since_value=custom_since_value,
        custom_until_value=custom_until_value,
        since_period=since_period,
        until_period=until_period,
        category_tag_tree=category_tag_tree,
        cat_since=cat_since,
        cat_until=cat_until,
        cat_period_key=cat_period_key,
        cat_custom_since_value=cat_custom_since_value,
        cat_custom_until_value=cat_custom_until_value,
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

    tags_by_category: dict[str, list[str]] = {}
    for t in core_db.get_tag_dictionary(conn, active_only=True):
        tags_by_category.setdefault(t["category"] or "без категории", []).append(t["tag"])
    for tags in tags_by_category.values():
        tags.sort()
    tags_by_category = dict(sorted(tags_by_category.items()))

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
        tags_by_category=tags_by_category,
    )


@app.route("/dashboard/<slug>/tags")
@login_required
def dashboard_tags(slug: str):
    """Вкладка «Словарь тегов» (2026-07-16) — дерево категория→теги (active) +
    approval-очередь новых тегов от модели снизу (см. PLAN.md "Approval новых
    тегов через Telegram" — решение перенести approval сюда, не в Telegram-кнопки,
    т.к. выбор категории кнопками упирается в лимит callback_data)."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return "Доступ запрещён", 403

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)

    category_descriptions = {c["category"]: c["description"] for c in core_db.get_category_dictionary(conn)}

    # category -> {"description": ..., "tags": [{"tag":..., "description":...}, ...]}
    tags_by_category: dict[str, dict] = {}
    for t in core_db.get_tag_dictionary(conn, active_only=True):
        cat = t["category"] or "без категории"
        bucket = tags_by_category.setdefault(cat, {"description": category_descriptions.get(cat, ""), "tags": []})
        bucket["tags"].append({"tag": t["tag"], "description": t["description"] or ""})
    for bucket in tags_by_category.values():
        bucket["tags"].sort(key=lambda t: t["tag"])
    tags_by_category = dict(sorted(tags_by_category.items()))

    all_categories = sorted(tags_by_category.keys())

    pending_tags = core_db.get_pending_tags(conn)
    for p in pending_tags:
        review_id = p.get("pending_review_id")
        p["review"] = None
        if review_id:
            row = conn.execute(
                "SELECT id, author, rating, text, platform, review_date FROM reviews WHERE id=?", (review_id,)
            ).fetchone()
            if row:
                review = dict(row)
                review["platform_label"] = PLATFORM_LABELS.get(review["platform"], review["platform"])
                review["tags"] = core_db.get_review_tags(conn, review_id)
                p["review"] = review

    active_tags_flat = sorted({t["tag"] for cat in tags_by_category.values() for t in cat["tags"]})

    conn.close()

    return render_template(
        "dashboard_tags.html",
        slug=slug,
        display_name=_client_display_name(slug),
        no_data=False,
        tags_by_category=tags_by_category,
        all_categories=all_categories,
        pending_tags=pending_tags,
        active_tags_flat=active_tags_flat,
    )


@app.route("/dashboard/<slug>/pending-tag/<tag>/approve", methods=["POST"])
@login_required
def approve_pending_tag(slug: str, tag: str):
    """Утвердить pending-тег как есть (см. dashboard_tags)."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return jsonify({"error": "Доступ запрещён"}), 403
    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    core_db.approve_tag(conn, tag)
    conn.close()
    return jsonify({"ok": True})


@app.route("/dashboard/<slug>/pending-tag/<tag>/approve-with-category", methods=["POST"])
@login_required
def approve_pending_tag_with_category(slug: str, tag: str):
    """Утвердить pending-тег с ИСПРАВЛЕННОЙ категорией — живой кейс: модель
    предложила тег "wifi" в категории "оснащение" (оснащение — это тренажёры/
    инвентарь для тренировок, wifi туда не подходит), см. CHANGELOG 2026-07-16."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return jsonify({"error": "Доступ запрещён"}), 403

    new_category = (request.form.get("category") or "").strip()
    if not new_category:
        return jsonify({"error": "Категория не указана"}), 400

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    known = {c["category"] for c in core_db.get_category_dictionary(conn)}
    if new_category not in known:
        conn.close()
        return jsonify({"error": f"Категория '{new_category}' не существует у этого клиента"}), 400
    core_db.approve_tag(conn, tag, category=new_category)
    conn.close()
    return jsonify({"ok": True})


@app.route("/dashboard/<slug>/pending-tag/<tag>/reject", methods=["POST"])
@login_required
def reject_pending_tag(slug: str, tag: str):
    """Отклонить pending-тег — удаляется из словаря целиком, уже размеченные
    review_tags не трогаются (см. core.db.reject_tag)."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return jsonify({"error": "Доступ запрещён"}), 403
    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    core_db.reject_tag(conn, tag)
    conn.close()
    return jsonify({"ok": True})


@app.route("/dashboard/<slug>/tag/<int:tag_row_id>", methods=["POST"])
@login_required
def update_tag(slug: str, tag_row_id: int):
    """Ручная коррекция тега на конкретном отзыве (2026-07-16) — владелец процесса
    видит, что модель ошиблась (например Wi-Fi-отзыв: "информирование" вместо
    "wifi", см. CHANGELOG 2026-07-16), и правит на месте. new_tag выбирается СТРОГО
    из активного словаря клиента (валидируется здесь) — это НЕ путь для добавления
    новых тегов мимо approval-флоу (см. PLAN.md "Approval новых тегов")."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return jsonify({"error": "Доступ запрещён"}), 403

    new_tag = (request.form.get("tag") or "").strip().lower()
    new_sentiment = request.form.get("tag_sentiment")
    if new_sentiment not in ("positive", "neutral", "negative"):
        return jsonify({"error": "Некорректная тональность"}), 400

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)

    active_tags = {t["tag"] for t in core_db.get_tag_dictionary(conn, active_only=True)}
    if new_tag not in active_tags:
        conn.close()
        return jsonify({"error": f"Тег '{new_tag}' не в активном словаре клиента"}), 400

    core_db.update_review_tag(conn, tag_row_id, new_tag, new_sentiment)
    conn.close()
    return jsonify({"ok": True, "tag": new_tag, "tag_sentiment": new_sentiment})


@app.route("/dashboard/<slug>/tag/<int:tag_row_id>/delete", methods=["POST"])
@login_required
def delete_tag(slug: str, tag_row_id: int):
    """Удалить тег с отзыва (2026-07-16) — модель поставила тег, которого в тексте
    вообще нет, править не на что, нужно просто убрать. Отдельный роут от update_tag
    (тот заменяет тег на другой, этот убирает без замены)."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return jsonify({"error": "Доступ запрещён"}), 403

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    core_db.delete_review_tag(conn, tag_row_id)
    conn.close()
    return jsonify({"ok": True})


@app.route("/dashboard/<slug>/review/<int:review_id>/tag", methods=["POST"])
@login_required
def add_tag(slug: str, review_id: int):
    """Добавить ДОПОЛНИТЕЛЬНЫЙ тег на отзыв (2026-07-16) — отзыв часто затрагивает
    несколько тем, а модель пропустила одну из них. Отличие от update_tag: не
    правит существующую строку, а вставляет новую — тег+тональность СТРОГО из
    активного словаря (та же валидация, что и в update_tag)."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return jsonify({"error": "Доступ запрещён"}), 403

    new_tag = (request.form.get("tag") or "").strip().lower()
    new_sentiment = request.form.get("tag_sentiment")
    if new_sentiment not in ("positive", "neutral", "negative"):
        return jsonify({"error": "Некорректная тональность"}), 400

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)

    active_tags = {t["tag"] for t in core_db.get_tag_dictionary(conn, active_only=True)}
    if new_tag not in active_tags:
        conn.close()
        return jsonify({"error": f"Тег '{new_tag}' не в активном словаре клиента"}), 400

    core_db.insert_review_tag(conn, review_id, new_tag, new_sentiment, "добавлено вручную")
    conn.close()
    return jsonify({"ok": True, "tag": new_tag, "tag_sentiment": new_sentiment})


@app.route("/dashboard/<slug>/recompute-alerts", methods=["POST"])
@login_required
def recompute_alerts_now(slug: str):
    """Кнопка "Пересчитать алерты сейчас" (2026-07-16) — после массовой ручной
    коррекции тегов не ждать планового main_alerts.py (раз в 6 часов, см. таймеры
    в README), увидеть эффект сразу. Использует ТЕ ЖЕ чистые функции ядра
    (agents.alert_engine), что и main_alerts.py — не дублирует логику."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return jsonify({"error": "Доступ запрещён"}), 403

    config_path = CLIENTS_DIR / slug / "client_config.yaml"
    cfg = core_load_config(path=config_path)

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)

    tag_changes = recompute_all(conn, cfg, core_db)
    zone_changes = recompute_zone_alerts(conn, cfg, core_db)
    offender_changes = recompute_repeat_offenders(conn, cfg, core_db)

    conn.close()
    return jsonify({
        "ok": True,
        "tag_changes": len(tag_changes),
        "zone_changes": len(zone_changes),
        "offender_changes": len(offender_changes),
    })


@app.route("/dashboard/<slug>/alert/<int:alert_id>/reviews")
@login_required
def alert_reviews_fragment(slug: str, alert_id: int):
    """Drill-down "алерт → сырые отзывы" для модалки на дашборде — HTML-фрагмент,
    не отдельная страница (см. CHANGELOG 2026-07-14: почему фрагмент, а не JSON —
    переиспользует уже готовую карточку отзыва, не дублирует логику отображения на JS)."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return "Доступ запрещён", 403

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)

    alert = core_db.get_alert_by_id(conn, alert_id)
    if alert is None:
        conn.close()
        return render_template("_alert_reviews_fragment.html", reviews=[], title="Алерт не найден")

    if alert["alert_type"] == "repeat_offender":
        _, author, platform = alert["tag"].split(":", 2)
        reviews = core_db.get_reviews_for_repeat_offender(conn, author, platform, alert["location_id"], alert["window_matched"])
        title = f"{author} · {PLATFORM_LABELS.get(platform, platform)}"
    elif alert["alert_type"] == "zone":
        reviews = core_db.get_reviews_for_zone_alert(conn, alert["tag"], alert["location_id"], alert["window_matched"])
        title = f"Зона: {alert['tag']}"
    else:
        reviews = core_db.get_reviews_for_tag_alert(conn, alert["tag"], alert["location_id"], alert["window_matched"])
        title = alert["tag"]

    for r in reviews:
        r["platform_label"] = PLATFORM_LABELS.get(r["platform"], r["platform"])

    conn.close()
    return render_template("_alert_reviews_fragment.html", reviews=reviews, title=title)


@app.route("/dashboard/<slug>/tag/<tag>/reviews")
@login_required
def tag_reviews_fragment(slug: str, tag: str):
    """Drill-down "тег в дереве категорий → сырые отзывы" (2026-07-20) — тот же
    паттерн HTML-фрагмента, что и alert_reviews_fragment. since/until — ТЕ ЖЕ
    границы периода, что дерево показывало на странице (передаются из JS как
    query-параметры, см. dashboard.html: openTagModal); при их отсутствии/битом
    формате (ручной заход на URL без JS) — тихий откат на дефолтный период, та
    же логика, что и у самого графика/дерева (period.resolve_period), не 400."""
    db_path = _require_client_access(slug)
    if db_path is None:
        return "Доступ запрещён", 403

    since_raw = request.args.get("since", "")
    until_raw = request.args.get("until", "")
    if since_raw and until_raw:
        since_iso, until_iso = since_raw, until_raw
    else:
        since_iso, until_iso, _, _ = period.resolve_period(DEFAULT_PERIOD)

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    location_id = _primary_location_id(conn)

    if location_id is None:
        conn.close()
        return render_template("_alert_reviews_fragment.html", reviews=[], title=tag)

    try:
        reviews = core_db.get_reviews_by_tag_since(conn, location_id, tag, since_iso, until_iso)
    except (ValueError, AttributeError):
        reviews = []

    for r in reviews:
        r["platform_label"] = PLATFORM_LABELS.get(r["platform"], r["platform"])

    conn.close()
    return render_template("_alert_reviews_fragment.html", reviews=reviews, title=tag)


@app.route("/telegram/webhook/<slug>", methods=["POST"])
def telegram_webhook(slug: str):
    """Приёмник callback_query от кнопки "Связался с клиентом" на repeat-offender
    уведомлениях (2026-07-16, заменяет polling — main_repeat_offender_poll.py).
    Telegram сам дёргает этот URL мгновенно при нажатии кнопки, задержки нет.

    НЕ за @login_required — это вызывает сам Telegram, не залогиненный пользователь.
    Секретность обеспечивается тем, что slug + сам факт валидного update от Telegram
    (подписан токеном бота при регистрации) достаточны для этого масштаба — нет
    отдельного secret_token в URL, риск: кто-то узнает URL и зашлёт поддельный
    update. Последствия ограничены — можно только "подтвердить" open алерт, не
    более (то же самое, что мог бы сделать любой сотрудник с доступом к чату)."""
    token = _client_bot_token(slug)
    if token is None:
        return jsonify({"ok": False, "error": "unknown client"}), 404

    update = request.get_json(silent=True) or {}
    cq = update.get("callback_query")
    if not cq or not cq.get("data", "").startswith("ro_ack:"):
        return jsonify({"ok": True})  # игнорируем любые другие апдейты молча

    db_path = _client_db_path(slug)
    if not db_path.exists():
        return jsonify({"ok": False, "error": "no db"}), 404

    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)

    alert_id = int(cq["data"].split(":", 1)[1])
    alert = core_db.get_alert_by_id(conn, alert_id)

    if alert is None or alert["status"] == "resolved":
        reply_text = "Этот алерт уже закрыт."
    elif alert["status"] == "acknowledged":
        reply_text = "Уже отмечено ранее."
    else:
        who = cq["from"].get("first_name") or cq["from"].get("username") or "неизвестно"
        core_db.acknowledge_alert(conn, alert_id, who)
        reply_text = "Спасибо, отмечено!"

    conn.close()

    try:
        answer_callback_query(cq["id"], reply_text, token=token)
    except Exception as e:
        print(f"[webhook {slug}] не удалось ответить на callback {cq['id']}: {e}")

    message = cq.get("message") or {}
    if message.get("message_id"):
        try:
            remove_message_keyboard(message["chat"]["id"], message["message_id"], token=token)
        except Exception as e:
            print(f"[webhook {slug}] не удалось убрать кнопку у сообщения {message['message_id']}: {e}")

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8789)
