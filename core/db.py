import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dates import parse_review_date
from .env import PROJECT_ROOT, get_client_root


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: Path = None) -> sqlite3.Connection:
    """db_path по умолчанию вычисляется лениво (не при импорте модуля) — иначе
    CLIENT_SLUG, установленный уже после импорта core.db, не учитывался бы."""
    db_path = db_path or (get_client_root() / "db" / "reviews.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reviews'"
    ).fetchone()
    if not existing:
        schema_path = PROJECT_ROOT / "db" / "schema.sql"
        with open(schema_path, encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Догоняет схему на уже существующих БД, созданных до появления описаний категорий/тегов."""
    has_category_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='category_dictionary'"
    ).fetchone()
    if not has_category_table:
        conn.execute(
            "CREATE TABLE category_dictionary (category TEXT PRIMARY KEY, description TEXT NOT NULL)"
        )

    tag_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tag_dictionary)")}
    if "description" not in tag_columns:
        conn.execute("ALTER TABLE tag_dictionary ADD COLUMN description TEXT")

    review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(reviews)")}
    if "notified_at" not in review_columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN notified_at TEXT")
    if "review_type" not in review_columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN review_type TEXT")
    if "internal_note" not in review_columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN internal_note TEXT")

    alert_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "alert_type" not in alert_columns:
        # 'tag' — старые/обычные тег-алерты (дефолт, обратная совместимость).
        # 'repeat_offender' — новый тип, см. get_active_repeat_offender_alert. Ключ
        # автора хранится в существующей колонке tag как "author:{author}:{platform}",
        # чтобы не переименовывать tag/location_id и не задевать весь текущий CRUD.
        conn.execute("ALTER TABLE alerts ADD COLUMN alert_type TEXT DEFAULT 'tag'")
    if "notify_count" not in alert_columns:
        # Сколько раз уже отправлено напоминание — пока нет inline-кнопки "Связался"
        # (см. PLAN.md), repeat offender останавливается сам после N напоминаний,
        # а не ждёт acknowledge вручную через CLI, который недоступен сервисной службе клиента.
        conn.execute("ALTER TABLE alerts ADD COLUMN notify_count INTEGER DEFAULT 0")

    conn.commit()


# ============================================================================
# Локации и площадки
# ============================================================================

def ensure_location(conn: sqlite3.Connection, location_id: str, name: str, city: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO locations (id, name, city) VALUES (?, ?, ?)",
        (location_id, name, city),
    )
    conn.commit()


def ensure_platform(conn: sqlite3.Connection, location_id: str, platform: str, url: str) -> None:
    row = conn.execute(
        "SELECT id FROM location_platforms WHERE location_id=? AND platform=?",
        (location_id, platform),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO location_platforms (location_id, platform, url) VALUES (?, ?, ?)",
            (location_id, platform, url),
        )
        conn.commit()


def update_platform_checkpoint(
    conn: sqlite3.Connection,
    location_id: str,
    platform: str,
    last_seen_review_id: str,
    last_seen_review_date: str,
) -> None:
    conn.execute(
        """UPDATE location_platforms
           SET last_seen_review_id=?, last_seen_review_date=?, last_checked_at=?
           WHERE location_id=? AND platform=?""",
        (last_seen_review_id, last_seen_review_date, now_iso(), location_id, platform),
    )
    conn.commit()


def get_locations(conn: sqlite3.Connection) -> list[str]:
    return [row["id"] for row in conn.execute("SELECT id FROM locations")]


def get_location_name(conn: sqlite3.Connection, location_id: str) -> str:
    row = conn.execute("SELECT name FROM locations WHERE id=?", (location_id,)).fetchone()
    return row["name"] if row else location_id


# ============================================================================
# Словарь тегов и категорий
# ============================================================================

def seed_tag_dictionary(conn: sqlite3.Connection, tags: list[dict]) -> None:
    """Upsert: категория/описание обновляются при правке client_config.yaml,
    статус (active/pending_review) не трогается — утверждение не должно откатываться."""
    for t in tags:
        conn.execute(
            """INSERT INTO tag_dictionary (tag, category, description, status)
               VALUES (?, ?, ?, 'active')
               ON CONFLICT(tag) DO UPDATE SET category=excluded.category, description=excluded.description""",
            (t["name"], t["category"], t.get("description", "")),
        )
    conn.commit()


def seed_category_dictionary(conn: sqlite3.Connection, categories: list[dict]) -> None:
    for c in categories:
        conn.execute(
            """INSERT INTO category_dictionary (category, description) VALUES (?, ?)
               ON CONFLICT(category) DO UPDATE SET description=excluded.description""",
            (c["name"], c["description"]),
        )
    conn.commit()


def get_tag_dictionary(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    query = "SELECT tag, category, description, status FROM tag_dictionary"
    if active_only:
        query += " WHERE status='active'"
    return [dict(row) for row in conn.execute(query)]


def get_category_dictionary(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT category, description FROM category_dictionary")]


def insert_tag_if_new(conn: sqlite3.Connection, tag: str, category: str = "не определено") -> bool:
    """Новый тег от модели попадает со статусом pending_review — не используется
    в active-словаре, пока кто-то не утвердит его вручную."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO tag_dictionary (tag, category, status) VALUES (?, ?, 'pending_review')",
        (tag, category),
    )
    conn.commit()
    return cur.rowcount > 0


# ============================================================================
# Отзывы
# ============================================================================

def insert_review_if_new(
    conn: sqlite3.Connection, location_id: str, platform: str, review: dict, skip_notify: bool = False
) -> bool:
    """review: {external_id, author, rating, text, date}. Возвращает True, если отзыв новый.

    skip_notify=True сразу проставляет notified_at — для backfill (первый крупный
    импорт истории), чтобы main_notify.py не разослал карточку по каждому вставленному
    отзыву при следующем плановом прогоне (см. main_collect.py --backfill)."""
    notified_at = now_iso() if skip_notify else None
    cur = conn.execute(
        """INSERT OR IGNORE INTO reviews
           (location_id, platform, external_review_id, author, rating, text, review_date, collected_at, reply_status, notified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            location_id,
            platform,
            review["external_id"],
            review.get("author"),
            review.get("rating"),
            review.get("text"),
            review.get("date"),
            now_iso(),
            review.get("reply_status", "pending"),
            notified_at,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def fetch_unanalyzed_reviews(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM reviews WHERE sentiment IS NULL").fetchall()


def update_review_sentiment(
    conn: sqlite3.Connection,
    review_id: int,
    sentiment: str,
    score: int,
    reasoning: str,
    urgency: bool,
    reply_sla_deadline: str | None,
) -> None:
    conn.execute(
        """UPDATE reviews SET sentiment=?, sentiment_score=?, sentiment_reasoning=?,
           urgency=?, reply_sla_deadline=? WHERE id=?""",
        (sentiment, score, reasoning, int(urgency), reply_sla_deadline, review_id),
    )
    conn.commit()


def insert_review_tag(conn: sqlite3.Connection, review_id: int, tag: str, tag_sentiment: str, tag_evidence: str) -> None:
    conn.execute(
        "INSERT INTO review_tags (review_id, tag, tag_sentiment, tag_evidence) VALUES (?, ?, ?, ?)",
        (review_id, tag, tag_sentiment, tag_evidence),
    )
    conn.commit()


def get_review_tags(conn: sqlite3.Connection, review_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT tag, tag_sentiment, tag_evidence FROM review_tags WHERE review_id=?", (review_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_negative_tag_events(conn: sqlite3.Connection) -> list[dict]:
    """Одна строка на (отзыв, тег) с негативной тональностью — основа для подсчёта окон.
    DISTINCT review_id учитывается на стороне alert_engine, не здесь."""
    rows = conn.execute(
        """SELECT r.location_id, rt.tag, rt.review_id, r.review_date
           FROM review_tags rt JOIN reviews r ON rt.review_id = r.id
           WHERE rt.tag_sentiment = 'negative'"""
    ).fetchall()
    return [dict(row) for row in rows]


_ANONYMOUS_AUTHOR_NAMES = {"anonymous review", "аноним", "анонимный отзыв"}


def get_negative_review_events_by_author(conn: sqlite3.Connection) -> list[dict]:
    """Одна строка на негативный ОТЗЫВ (не тег внутри него) с известным автором —
    основа для repeat offender. author+platform — намеренно НЕ по всей БД сразу:
    одинаковое имя на разных площадках не гарантированно один человек (см. PLAN.md).
    Площадки иногда отдают литерал вместо реального имени (например "Anonymous
    review" на Яндекс.Картах) — разные люди попали бы под один ключ, исключаем явно."""
    rows = conn.execute(
        """SELECT location_id, platform, author, id AS review_id, review_date
           FROM reviews
           WHERE sentiment = 'negative' AND author IS NOT NULL AND author != ''"""
    ).fetchall()
    rows = [r for r in rows if r["author"].strip().lower() not in _ANONYMOUS_AUTHOR_NAMES]
    return [dict(row) for row in rows]


def get_reviews_needing_reply(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reviews WHERE reply_status='pending' AND reply_draft IS NULL AND sentiment IS NOT NULL"
    ).fetchall()


def update_reply_draft(conn: sqlite3.Connection, review_id: int, review_type: str, reply_draft: str, internal_note: str | None) -> None:
    conn.execute(
        "UPDATE reviews SET review_type=?, reply_draft=?, internal_note=? WHERE id=?",
        (review_type, reply_draft, internal_note, review_id),
    )
    conn.commit()


def get_overdue_reviews(conn: sqlite3.Connection, recent_cutoff_days: int = 90) -> list[sqlite3.Row]:
    """Просроченные по SLA отзывы моложе recent_cutoff_days (по review_date) — для
    ежедневного watchdog. Старые просрочки уходят в get_stale_overdue_reviews, чтобы
    один и тот же полугодовой "хвост" не всплывал в срочных уведомлениях бесконечно."""
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_cutoff_days)).isoformat()
    return conn.execute(
        """SELECT * FROM reviews
           WHERE reply_status='pending' AND reply_sla_deadline IS NOT NULL
           AND reply_sla_deadline < ? AND review_date >= ?""",
        (now_iso(), recent_cutoff),
    ).fetchall()


def get_stale_overdue_reviews(
    conn: sqlite3.Connection, recent_cutoff_days: int = 90, stale_cutoff_days: int = 180
) -> list[sqlite3.Row]:
    """Просроченные отзывы в возрасте [recent_cutoff_days; stale_cutoff_days) по
    review_date — уже не срочные, но ещё не списаны совсем. Раз в неделю напоминаем
    списком, не спамим ежедневно."""
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_cutoff_days)).isoformat()
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_cutoff_days)).isoformat()
    return conn.execute(
        """SELECT * FROM reviews
           WHERE reply_status='pending' AND reply_sla_deadline IS NOT NULL
           AND reply_sla_deadline < ? AND review_date < ? AND review_date >= ?""",
        (now_iso(), recent_cutoff, stale_cutoff),
    ).fetchall()


def get_unnotified_reviews(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reviews WHERE sentiment IS NOT NULL AND notified_at IS NULL"
    ).fetchall()


def mark_notified(conn: sqlite3.Connection, review_id: int) -> None:
    conn.execute("UPDATE reviews SET notified_at=? WHERE id=?", (now_iso(), review_id))
    conn.commit()


def get_review_sentiment_counts_since(conn: sqlite3.Connection, location_id: str, since_iso: str) -> dict:
    """Считаем по review_date (когда клиент реально написал отзыв), а не по
    notified_at/collected_at — оба этих поля при backfill проставляются "сейчас" всей
    пачкой (мы обработали 7 месяцев истории за один прогон), и любое из них выдало бы
    "78 новых отзывов за сутки" вместо честных "0 сегодня, это старая история"."""
    since_dt = parse_review_date(since_iso)
    rows = conn.execute(
        "SELECT sentiment, review_date FROM reviews WHERE location_id=? AND review_date IS NOT NULL",
        (location_id,),
    ).fetchall()

    counts: dict[str, int] = {}
    for row in rows:
        try:
            dt = parse_review_date(row["review_date"])
        except (ValueError, AttributeError):
            continue
        if dt >= since_dt:
            counts[row["sentiment"]] = counts.get(row["sentiment"], 0) + 1
    return counts


# ============================================================================
# Алерты
# ============================================================================

def get_active_alert(conn: sqlite3.Connection, tag: str, location_id: str) -> sqlite3.Row | None:
    """Один конкретный (тег, точка) — используется в Alert Engine при пересчёте."""
    return conn.execute(
        "SELECT * FROM alerts WHERE tag=? AND location_id=? AND status != 'resolved'",
        (tag, location_id),
    ).fetchone()


def get_active_alerts_for_tags(conn: sqlite3.Connection, location_id: str, tags: list[str]) -> list[dict]:
    """Несколько тегов сразу на одной точке — используется Reply Strategist, чтобы
    подмешать контекст алерта во внутреннюю заметку менеджеру."""
    if not tags:
        return []
    placeholders = ",".join("?" * len(tags))
    rows = conn.execute(
        f"""SELECT * FROM alerts WHERE location_id=? AND status != 'resolved'
            AND tag IN ({placeholders})""",
        (location_id, *tags),
    ).fetchall()
    return [dict(row) for row in rows]


def get_all_active_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Все незакрытые алерты (open+acknowledged) по всем тегам/точкам — для дайджеста и CLI acknowledge."""
    return conn.execute(
        "SELECT * FROM alerts WHERE status != 'resolved' ORDER BY severity DESC, first_triggered_at"
    ).fetchall()


def get_alert_by_id(conn: sqlite3.Connection, alert_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()


def create_alert(
    conn: sqlite3.Connection, tag: str, location_id: str, severity: str, window_matched: int, count_in_window: int
) -> None:
    conn.execute(
        """INSERT INTO alerts (tag, location_id, severity, window_matched, count_in_window, first_triggered_at, status)
           VALUES (?, ?, ?, ?, ?, ?, 'open')""",
        (tag, location_id, severity, window_matched, count_in_window, now_iso()),
    )
    conn.commit()


def update_alert_severity(conn: sqlite3.Connection, alert_id: int, severity: str, window_matched: int, count_in_window: int) -> None:
    """Статус (open/acknowledged) не трогаем — человек мог уже взять в работу."""
    conn.execute(
        "UPDATE alerts SET severity=?, window_matched=?, count_in_window=? WHERE id=?",
        (severity, window_matched, count_in_window, alert_id),
    )
    conn.commit()


def acknowledge_alert(conn: sqlite3.Connection, alert_id: int, acknowledged_by: str | None) -> None:
    conn.execute(
        "UPDATE alerts SET status='acknowledged', acknowledged_by=?, acknowledged_at=? WHERE id=?",
        (acknowledged_by, now_iso(), alert_id),
    )
    conn.commit()


# ----------------------------------------------------------------------------
# Repeat offender — тот же CRUD выше (create_alert/update_alert_severity/
# acknowledge_alert/resolve_alert) работает по alert.id и не зависит от типа,
# переиспользуется без изменений. Ключ "автор+площадка" хранится в колонке tag
# как "author:{author}:{platform}" (см. _migrate) — отдельные хелперы ниже только
# для построения/поиска по этому ключу и для повторных напоминаний.
# ----------------------------------------------------------------------------

def repeat_offender_key(author: str, platform: str) -> str:
    return f"author:{author}:{platform}"


def get_active_repeat_offender_alert(conn: sqlite3.Connection, author: str, platform: str, location_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alerts WHERE tag=? AND location_id=? AND alert_type='repeat_offender' AND status != 'resolved'",
        (repeat_offender_key(author, platform), location_id),
    ).fetchone()


def create_repeat_offender_alert(
    conn: sqlite3.Connection, author: str, platform: str, location_id: str, severity: str, window_matched: int, count_in_window: int
) -> int:
    cur = conn.execute(
        """INSERT INTO alerts (tag, location_id, severity, window_matched, count_in_window, first_triggered_at, status, alert_type)
           VALUES (?, ?, ?, ?, ?, ?, 'open', 'repeat_offender')""",
        (repeat_offender_key(author, platform), location_id, severity, window_matched, count_in_window, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def get_all_active_repeat_offender_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts WHERE alert_type='repeat_offender' AND status != 'resolved' ORDER BY first_triggered_at"
    ).fetchall()


def get_repeat_offender_alerts_due_for_renotify(conn: sqlite3.Connection, renotify_after_days: int, max_notify_count: int) -> list[sqlite3.Row]:
    """Открытые (НЕ acknowledged — человек уже взял в работу, повторно не дёргаем)
    repeat-offender алерты, которым пора напомнить снова: либо ни разу не уведомляли
    после создания, либо последнее уведомление было раньше порога. Останавливается
    после max_notify_count отправок — нет кнопки "Связался" для сервисной службы
    клиента (см. PLAN.md), бесконечно напоминать без возможности подтвердить контакт
    было бы просто спамом."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=renotify_after_days)).isoformat()
    return conn.execute(
        """SELECT * FROM alerts
           WHERE alert_type='repeat_offender' AND status='open'
           AND notify_count < ?
           AND (last_notified_at IS NULL OR last_notified_at < ?)""",
        (max_notify_count, cutoff),
    ).fetchall()


def mark_alert_notified(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute(
        "UPDATE alerts SET last_notified_at=?, notify_count=notify_count+1 WHERE id=?",
        (now_iso(), alert_id),
    )
    conn.commit()


def resolve_alert(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute(
        "UPDATE alerts SET status='resolved', resolved_at=? WHERE id=?",
        (now_iso(), alert_id),
    )
    conn.commit()


# ============================================================================
# Еженедельная сводка (main_weekly_summary.py) — итоги ЗА ПЕРИОД (7 дней), в отличие
# от main_digest.py (снимок текущего состояния на момент запуска)
# ============================================================================

def get_alerts_opened_since(conn: sqlite3.Connection, location_id: str, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts WHERE location_id=? AND first_triggered_at >= ? ORDER BY first_triggered_at",
        (location_id, since_iso),
    ).fetchall()


def get_alerts_resolved_since(conn: sqlite3.Connection, location_id: str, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts WHERE location_id=? AND resolved_at >= ? ORDER BY resolved_at",
        (location_id, since_iso),
    ).fetchall()


def get_top_tags_by_sentiment_since(
    conn: sqlite3.Connection, location_id: str, since_iso: str, sentiment: str, limit: int = 3, min_count: int = 2
) -> list[dict]:
    """Топ тегов ОТДЕЛЬНО по знаку (sentiment='positive'|'negative') за период —
    руководителю важно не "что вообще обсуждали", а "что хвалят" и "что ругают"
    раздельно, иначе тег без знака ничего не говорит о том, чинить или хвалить.

    min_count отсекает шум: при малом числе отзывов за неделю тема с 1 упоминанием
    статистически неотличима от случайности — не тема "топ", а совпадение. Если ни
    одна тема не набрала min_count, возвращается пустой список (вызывающая сторона
    должна показать "недостаточно данных", не подсовывать случайные темы как топ)."""
    since_dt = parse_review_date(since_iso)
    rows = conn.execute(
        """SELECT rt.tag, r.review_date FROM review_tags rt
           JOIN reviews r ON rt.review_id = r.id
           WHERE r.location_id = ? AND r.review_date IS NOT NULL AND rt.tag_sentiment = ?""",
        (location_id, sentiment),
    ).fetchall()

    counts: dict[str, int] = {}
    for row in rows:
        try:
            dt = parse_review_date(row["review_date"])
        except (ValueError, AttributeError):
            continue
        if dt >= since_dt:
            counts[row["tag"]] = counts.get(row["tag"], 0) + 1

    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"tag": tag, "count": count} for tag, count in top if count >= min_count]


def get_overdue_reviews_since(conn: sqlite3.Connection, location_id: str, since_iso: str) -> list[sqlite3.Row]:
    """Отзывы, чей дедлайн ответа попал в период (since_iso .. сейчас) и всё ещё не
    отвечены — для еженедельной сводки "сколько просрочек случилось на этой неделе",
    в отличие от main_watchdog.py (все текущие просрочки независимо от того, когда
    наступил дедлайн)."""
    return conn.execute(
        """SELECT * FROM reviews
           WHERE location_id=? AND reply_status='pending' AND reply_sla_deadline IS NOT NULL
           AND reply_sla_deadline >= ? AND reply_sla_deadline < ?""",
        (location_id, since_iso, now_iso()),
    ).fetchall()


# ============================================================================
# Веб-дашборд (webapp/) — v1, только на данных, которые уже считаются выше
# (см. PLAN.md "Дашборд клиента v1/v2" — что осталось в v2 и почему)
# ============================================================================

def get_platform_comparison_since(conn: sqlite3.Connection, location_id: str, since_iso: str) -> list[dict]:
    """Объём и доля негатива по площадке за период — "куда направить усилия по
    ответам" (см. PLAN.md, документ Perplexity — Platform Comparison). Фильтрация по
    дате в Python, не в SQL — те же причины, что и в get_review_sentiment_counts_since
    (разные форматы дат/смещений между площадками, ненадёжно сравнивать строками)."""
    since_dt = parse_review_date(since_iso)
    rows = conn.execute(
        "SELECT platform, sentiment, review_date FROM reviews WHERE location_id=? AND review_date IS NOT NULL",
        (location_id,),
    ).fetchall()

    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        try:
            dt = parse_review_date(row["review_date"])
        except (ValueError, AttributeError):
            continue
        if dt < since_dt:
            continue
        platform = row["platform"]
        stats.setdefault(platform, {"total": 0, "negative": 0})
        stats[platform]["total"] += 1
        if row["sentiment"] == "negative":
            stats[platform]["negative"] += 1

    result = []
    for platform, s in stats.items():
        negative_share = round(100 * s["negative"] / s["total"]) if s["total"] else 0
        result.append({"platform": platform, "total": s["total"], "negative": s["negative"], "negative_share_pct": negative_share})
    return sorted(result, key=lambda r: r["total"], reverse=True)


def get_hidden_problems(conn: sqlite3.Connection, location_id: str, min_rating: int = 4, limit: int = 10) -> list[dict]:
    """Отзывы с высокой оценкой (min_rating+ звёзд), но с негативным тегом внутри —
    "Sentiment vs Rating mismatch" (см. PLAN.md). Aspect-based теги это уже хранят —
    только запрос, без новой аналитики. Клиент ставит 5★ по привычке/вежливости, но
    текст содержит реальную проблему, которую общая оценка полностью скрывает."""
    rows = conn.execute(
        """SELECT DISTINCT r.id, r.author, r.rating, r.text, r.platform, r.review_date,
                  GROUP_CONCAT(rt.tag, ', ') AS negative_tags
           FROM reviews r
           JOIN review_tags rt ON rt.review_id = r.id
           WHERE r.location_id = ? AND r.rating >= ? AND rt.tag_sentiment = 'negative'
           GROUP BY r.id
           ORDER BY r.review_date DESC
           LIMIT ?""",
        (location_id, min_rating, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_reviews_paginated(
    conn: sqlite3.Connection, location_id: str, platform: str | None = None,
    sentiment: str | None = None, offset: int = 0, limit: int = 20,
) -> tuple[list[dict], int]:
    """Лента отзывов с фильтрами для вкладки "Лента отзывов" — возвращает (страница,
    общее количество под фильтром) для пагинации на стороне webapp."""
    where = ["location_id = ?"]
    params: list = [location_id]
    if platform:
        where.append("platform = ?")
        params.append(platform)
    if sentiment:
        where.append("sentiment = ?")
        params.append(sentiment)
    where_sql = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) AS c FROM reviews WHERE {where_sql}", params).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT * FROM reviews WHERE {where_sql}
            ORDER BY review_date DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows], total


def get_reviews_for_tag_alert(conn: sqlite3.Connection, tag: str, location_id: str, window_days: int) -> list[dict]:
    """Drill-down "алерт → сырые отзывы": те же негативные отзывы с этим тегом за
    window_days, что и увидел бы recompute_all при пересчёте severity (alert_engine
    хранит только агрегированный счётчик, не конкретные review_id — пересчитываем
    заново на актуальный момент, а не замораживаем список на момент срабатывания)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """SELECT DISTINCT r.id, r.author, r.rating, r.text, r.platform, r.review_date, rt.tag_evidence
           FROM reviews r JOIN review_tags rt ON rt.review_id = r.id
           WHERE r.location_id = ? AND rt.tag = ? AND rt.tag_sentiment = 'negative' AND r.review_date >= ?
           ORDER BY r.review_date DESC""",
        (location_id, tag, cutoff),
    ).fetchall()
    return [dict(row) for row in rows]


def get_reviews_for_repeat_offender(conn: sqlite3.Connection, author: str, platform: str, location_id: str, window_days: int) -> list[dict]:
    """Drill-down для repeat-offender алерта — негативные отзывы конкретного автора
    на конкретной площадке за то же окно, что использовал compute_repeat_offenders."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """SELECT id, author, rating, text, platform, review_date
           FROM reviews
           WHERE location_id = ? AND author = ? AND platform = ? AND sentiment = 'negative' AND review_date >= ?
           ORDER BY review_date DESC""",
        (location_id, author, platform, cutoff),
    ).fetchall()
    return [dict(row) for row in rows]


def get_review_by_id(conn: sqlite3.Connection, review_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
