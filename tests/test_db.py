"""db.py на изолированной SQLite в памяти — не трогает реальную db/reviews.db."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core import db


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    db.ensure_location(c, "loc1", "Тестовый клуб", "Тюмень")
    yield c
    c.close()


def test_insert_review_if_new_is_idempotent(conn):
    review = {"external_id": "abc123", "author": "Аня", "rating": 5, "text": "Отлично", "date": None}
    assert db.insert_review_if_new(conn, "loc1", "yandex_maps", review) is True
    assert db.insert_review_if_new(conn, "loc1", "yandex_maps", review) is False  # повторная вставка — молча игнорируется
    rows = conn.execute("SELECT COUNT(*) as n FROM reviews").fetchone()
    assert rows["n"] == 1


def test_insert_review_skip_notify_marks_notified_immediately(conn):
    """Регрессия (2026-07-08, Даудель Спорт): backfill без skip_notify оставляет
    notified_at=NULL, и main_notify.py потом рассылает карточку по каждому вставленному
    отзыву одним пакетом при следующем плановом прогоне — 90 сообщений разом."""
    normal = {"external_id": "n1", "author": None, "rating": 5, "text": "т", "date": None}
    backfilled = {"external_id": "b1", "author": None, "rating": 5, "text": "т", "date": None}

    db.insert_review_if_new(conn, "loc1", "yandex_maps", normal)
    db.insert_review_if_new(conn, "loc1", "yandex_maps", backfilled, skip_notify=True)

    conn.execute("UPDATE reviews SET sentiment='positive'")  # имитация main_analyze.py
    conn.commit()

    unnotified_ids = {r["external_review_id"] for r in db.get_unnotified_reviews(conn)}
    assert unnotified_ids == {"n1"}  # backfilled уже помечен, main_notify.py его не тронет


def test_alert_lifecycle_open_update_resolve(conn):
    db.create_alert(conn, "бассейн", "loc1", "yellow", 30, 3)
    alert = db.get_active_alert(conn, "бассейн", "loc1")
    assert alert["status"] == "open"

    db.update_alert_severity(conn, alert["id"], "red", 30, 5)
    alert = db.get_active_alert(conn, "бассейн", "loc1")
    assert alert["severity"] == "red"
    assert alert["status"] == "open"  # update не должен трогать статус

    db.acknowledge_alert(conn, alert["id"], "Жорж")
    alert = db.get_active_alert(conn, "бассейн", "loc1")
    assert alert["status"] == "acknowledged"
    assert alert["acknowledged_by"] == "Жорж"

    db.resolve_alert(conn, alert["id"])
    assert db.get_active_alert(conn, "бассейн", "loc1") is None  # закрытый алерт больше не "активный"


def test_review_sentiment_counts_since_uses_review_date(conn):
    """Регрессия: раньше считалось по notified_at, которое при backfill проставляется
    "сейчас" всей пачкой — старые отзывы ложно попадали в "за последние сутки"."""
    now = datetime.now(timezone.utc)
    old_review = {
        "external_id": "old1", "author": None, "rating": 5, "text": "старый",
        "date": (now - timedelta(days=200)).isoformat(),
    }
    fresh_review = {
        "external_id": "new1", "author": None, "rating": 4, "text": "новый",
        "date": (now - timedelta(hours=2)).isoformat(),
    }
    db.insert_review_if_new(conn, "loc1", "yandex_maps", old_review)
    db.insert_review_if_new(conn, "loc1", "yandex_maps", fresh_review)
    # оба помечаем notified "сейчас" (имитация backfill) — старый не должен попасть в подсчёт
    conn.execute("UPDATE reviews SET sentiment='positive', notified_at=?", (now.isoformat(),))
    conn.commit()

    since = (now - timedelta(hours=24)).isoformat()
    counts = db.get_review_sentiment_counts_since(conn, "loc1", since)
    assert counts.get("positive", 0) == 1  # только fresh_review, старый не в счёт


def _insert_overdue(conn, external_id: str, days_old: int, now: datetime) -> int:
    """Отзыв с review_date days_old дней назад, дедлайн ответа уже просрочен (вчера)."""
    review = {
        "external_id": external_id, "author": None, "rating": 2, "text": "плохо",
        "date": (now - timedelta(days=days_old)).isoformat(),
    }
    db.insert_review_if_new(conn, "loc1", "yandex_maps", review)
    review_id = conn.execute(
        "SELECT id FROM reviews WHERE external_review_id=?", (external_id,)
    ).fetchone()["id"]
    deadline = (now - timedelta(days=1)).isoformat()
    db.update_review_sentiment(conn, review_id, "negative", 8, "тест", False, deadline)
    return review_id


def test_overdue_reviews_split_by_age(conn):
    """Регрессия: раньше get_overdue_reviews возвращал ВСЕ просрочки без верхней границы —
    полугодовой "хвост" слался бы в watchdog заново каждый прогон 6-часового цикла."""
    now = datetime.now(timezone.utc)
    recent_id = _insert_overdue(conn, "recent", days_old=30, now=now)     # в пределах 90 дней
    stale_id = _insert_overdue(conn, "stale", days_old=120, now=now)      # 90-180 дней
    ancient_id = _insert_overdue(conn, "ancient", days_old=250, now=now)  # старше 180 дней

    recent_ids = {r["id"] for r in db.get_overdue_reviews(conn, recent_cutoff_days=90)}
    stale_ids = {r["id"] for r in db.get_stale_overdue_reviews(conn, recent_cutoff_days=90, stale_cutoff_days=180)}

    assert recent_ids == {recent_id}
    assert stale_ids == {stale_id}
    assert ancient_id not in recent_ids
    assert ancient_id not in stale_ids  # старше 180 дней — нигде не всплывает в уведомлениях

    # но отзыв остаётся в БД как pending — для статистики в финальном дашборде
    row = conn.execute("SELECT reply_status FROM reviews WHERE id=?", (ancient_id,)).fetchone()
    assert row["reply_status"] == "pending"


def test_top_tags_by_sentiment_filters_location_period_and_sentiment(conn):
    """Топ тегов должен учитывать только эту локацию, только отзывы внутри периода
    (review_date) и только нужный знак тональности — руководителю нужно раздельно
    "что хвалят" и "что ругают", смешивать позитив с негативом в одном счёте нельзя."""
    now = datetime.now(timezone.utc)
    db.ensure_location(conn, "loc2", "Другой клуб", "Москва")

    def _review(external_id, days_old, loc="loc1"):
        r = {"external_id": external_id, "author": None, "rating": 4, "text": "т",
             "date": (now - timedelta(days=days_old)).isoformat()}
        db.insert_review_if_new(conn, loc, "yandex_maps", r)
        return conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]

    fresh1 = _review("t1", days_old=1)
    fresh2 = _review("t2", days_old=2)
    old = _review("t3", days_old=30)  # старше окна
    other_loc = _review("t4", days_old=1, loc="loc2")  # другая локация
    negative_review = _review("t5", days_old=1)

    db.insert_review_tag(conn, fresh1, "бассейн", "positive", "evidence")
    db.insert_review_tag(conn, fresh2, "бассейн", "positive", "evidence")
    db.insert_review_tag(conn, old, "бассейн", "positive", "evidence")  # вне окна — не в счёт
    db.insert_review_tag(conn, other_loc, "бассейн", "positive", "evidence")  # чужая точка — не в счёт
    db.insert_review_tag(conn, negative_review, "бассейн", "negative", "evidence")  # другой знак — не в позитив

    since = (now - timedelta(days=7)).isoformat()
    top_positive = db.get_top_tags_by_sentiment_since(conn, "loc1", since, "positive", limit=3, min_count=2)

    assert top_positive == [{"tag": "бассейн", "count": 2}]


def test_top_tags_by_sentiment_below_min_count_is_empty(conn):
    """Регрессия на реальную жалобу: 3 темы с 1 упоминанием каждая — это шум, не топ.
    Функция должна честно вернуть пусто, а не подсунуть случайные темы как "топ"."""
    now = datetime.now(timezone.utc)
    r = {"external_id": "single", "author": None, "rating": 5, "text": "т", "date": now.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
    review_id = conn.execute("SELECT id FROM reviews WHERE external_review_id='single'").fetchone()["id"]
    db.insert_review_tag(conn, review_id, "групповые программы", "positive", "evidence")

    since = (now - timedelta(days=7)).isoformat()
    top = db.get_top_tags_by_sentiment_since(conn, "loc1", since, "positive", limit=3, min_count=2)

    assert top == []


def test_tag_counts_by_category_since_groups_and_sums(conn):
    """Дерево категория→тег (2026-07-20, дашборд): группировка правильная, счётчики
    на уровне категории и тега считаются раздельно по знаку, без порога min_count
    (в отличие от get_top_tags_by_sentiment_since — здесь нужна полная картина,
    не топ-N)."""
    now = datetime.now(timezone.utc)
    db.seed_tag_dictionary(conn, [
        {"name": "оборудование", "category": "тренажёрный зал", "description": ""},
        {"name": "чистота", "category": "тренажёрный зал", "description": ""},
        {"name": "бассейн", "category": "бассейн и сауна", "description": ""},
    ])

    def _review(external_id):
        r = {"external_id": external_id, "author": None, "rating": 4, "text": "т", "date": now.isoformat()}
        db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
        return conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]

    r1, r2, r3, r4 = _review("c1"), _review("c2"), _review("c3"), _review("c4")
    db.insert_review_tag(conn, r1, "оборудование", "positive", "evidence")
    db.insert_review_tag(conn, r2, "оборудование", "negative", "evidence")
    db.insert_review_tag(conn, r3, "чистота", "positive", "evidence")
    db.insert_review_tag(conn, r4, "бассейн", "negative", "evidence")

    since = (now - timedelta(days=7)).isoformat()
    until = (now + timedelta(days=1)).isoformat()
    tree = db.get_tag_counts_by_category_since(conn, "loc1", since, until)

    by_cat = {c["category"]: c for c in tree}
    assert by_cat["тренажёрный зал"]["total"] == 3
    assert by_cat["тренажёрный зал"]["positive"] == 2
    assert by_cat["тренажёрный зал"]["negative"] == 1
    assert by_cat["бассейн и сауна"]["total"] == 1

    tags_by_name = {t["tag"]: t for t in by_cat["тренажёрный зал"]["tags"]}
    assert tags_by_name["оборудование"] == {"tag": "оборудование", "total": 2, "positive": 1, "neutral": 0, "negative": 1}
    assert tags_by_name["чистота"]["total"] == 1

    # Сортировка: категория с бОльшим total — первая
    assert tree[0]["category"] == "тренажёрный зал"


def test_tag_counts_by_category_since_respects_period_bounds(conn):
    now = datetime.now(timezone.utc)
    db.seed_tag_dictionary(conn, [{"name": "цена", "category": "коммерция", "description": ""}])

    def _review(external_id, days_old):
        r = {"external_id": external_id, "author": None, "rating": 3, "text": "т",
             "date": (now - timedelta(days=days_old)).isoformat()}
        db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
        return conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]

    fresh = _review("p1", days_old=1)
    old = _review("p2", days_old=60)  # вне периода
    db.insert_review_tag(conn, fresh, "цена", "negative", "evidence")
    db.insert_review_tag(conn, old, "цена", "negative", "evidence")

    since = (now - timedelta(days=7)).isoformat()
    until = now.isoformat()
    tree = db.get_tag_counts_by_category_since(conn, "loc1", since, until)

    assert tree[0]["total"] == 1  # старый отзыв не попал


def test_tag_counts_by_category_since_unknown_tag_falls_back_to_no_category(conn):
    """Тег без записи в tag_dictionary (например отклонённый после разметки —
    reject_tag не трогает уже размеченные review_tags) не должен пропадать из
    дерева — падает в fallback "без категории", тот же паттерн, что уже
    используется в webapp/app.py для отображения словаря тегов."""
    now = datetime.now(timezone.utc)
    r = {"external_id": "u1", "author": None, "rating": 3, "text": "т", "date": now.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
    review_id = conn.execute("SELECT id FROM reviews WHERE external_review_id='u1'").fetchone()["id"]
    db.insert_review_tag(conn, review_id, "тег-сирота", "neutral", "evidence")

    since = (now - timedelta(days=7)).isoformat()
    until = now.isoformat()
    tree = db.get_tag_counts_by_category_since(conn, "loc1", since, until)

    assert tree[0]["category"] == "без категории"
    assert tree[0]["tags"][0]["tag"] == "тег-сирота"


def test_reviews_by_tag_since_includes_all_sentiments_and_period_bounds(conn):
    """В отличие от get_reviews_for_tag_alert (только negative, окно алерта) —
    дерево показывает разбивку по всем трём знакам, поэтому drill-down должен
    отдавать отзывы любого знака тега, в границах ТЕКУЩЕГО периода дерева."""
    now = datetime.now(timezone.utc)

    def _review(external_id, days_old):
        r = {"external_id": external_id, "author": "А", "rating": 5, "text": "т",
             "date": (now - timedelta(days=days_old)).isoformat()}
        db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
        return conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]

    positive = _review("d1", days_old=1)
    negative = _review("d2", days_old=2)
    out_of_range = _review("d3", days_old=60)
    db.insert_review_tag(conn, positive, "wifi", "positive", "evidence")
    db.insert_review_tag(conn, negative, "wifi", "negative", "evidence")
    db.insert_review_tag(conn, out_of_range, "wifi", "negative", "evidence")

    since = (now - timedelta(days=7)).isoformat()
    until = now.isoformat()
    reviews = db.get_reviews_by_tag_since(conn, "loc1", "wifi", since, until)

    ids = {r["id"] for r in reviews}
    assert ids == {positive, negative}  # позитив включён, старый отзыв — нет


def test_reviews_by_tag_since_dedupes_multi_evidence(conn):
    """Один отзыв — одна карточка, даже если тег стоит на нескольких цитатах
    (та же гарантия, что и у get_reviews_for_tag_alert)."""
    now = datetime.now(timezone.utc)
    r = {"external_id": "dup1", "author": None, "rating": 4, "text": "т", "date": now.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
    review_id = conn.execute("SELECT id FROM reviews WHERE external_review_id='dup1'").fetchone()["id"]
    db.insert_review_tag(conn, review_id, "сервис", "negative", "первая цитата")
    db.insert_review_tag(conn, review_id, "сервис", "negative", "вторая цитата")

    since = (now - timedelta(days=7)).isoformat()
    until = now.isoformat()
    reviews = db.get_reviews_by_tag_since(conn, "loc1", "сервис", since, until)

    assert len(reviews) == 1


def test_alerts_opened_and_resolved_since(conn):
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat()

    db.create_alert(conn, "цена", "loc1", "yellow", 30, 3)  # открыт только что — попадает в окно
    conn.execute(
        "UPDATE alerts SET first_triggered_at=? WHERE tag='услуги'",
        ((now - timedelta(days=30)).isoformat(),),
    )  # алерт вне окна (создан 30 дней назад)
    db.create_alert(conn, "услуги", "loc1", "yellow", 30, 3)
    old_alert_id = conn.execute("SELECT id FROM alerts WHERE tag='услуги'").fetchone()["id"]
    conn.execute(
        "UPDATE alerts SET first_triggered_at=? WHERE id=?",
        ((now - timedelta(days=30)).isoformat(), old_alert_id),
    )
    conn.commit()

    db.create_alert(conn, "персонал", "loc1", "red", 30, 5)
    personal_id = conn.execute("SELECT id FROM alerts WHERE tag='персонал'").fetchone()["id"]
    db.resolve_alert(conn, personal_id)  # закрыт только что — попадает в окно

    opened = db.get_alerts_opened_since(conn, "loc1", since)
    resolved = db.get_alerts_resolved_since(conn, "loc1", since)

    assert {a["tag"] for a in opened} == {"цена", "персонал"}  # услуги создан давно — не в окне
    assert {a["tag"] for a in resolved} == {"персонал"}


def test_overdue_reviews_since_window(conn):
    """Просрочки за неделю — дедлайн должен попасть именно в окно since..сейчас,
    а не быть просрочкой вообще (это отдельная функция get_overdue_reviews)."""
    now = datetime.now(timezone.utc)

    def _review_with_deadline(external_id, deadline_days_ago):
        r = {"external_id": external_id, "author": None, "rating": 2, "text": "т",
             "date": (now - timedelta(days=deadline_days_ago + 1)).isoformat()}
        db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
        rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
        deadline = (now - timedelta(days=deadline_days_ago)).isoformat()
        db.update_review_sentiment(conn, rid, "negative", 8, "т", False, deadline)
        return rid

    in_window = _review_with_deadline("dw1", deadline_days_ago=3)   # дедлайн 3 дня назад — в окне недели
    out_of_window = _review_with_deadline("dw2", deadline_days_ago=20)  # дедлайн 20 дней назад — вне окна

    since = (now - timedelta(days=7)).isoformat()
    overdue = db.get_overdue_reviews_since(conn, "loc1", since)
    ids = {r["id"] for r in overdue}

    assert in_window in ids
    assert out_of_window not in ids


def test_platform_comparison_since_splits_by_platform_and_computes_negative_share(conn):
    now = datetime.now(timezone.utc)

    def _review(external_id, platform, sentiment, days_old=1):
        r = {"external_id": external_id, "author": None, "rating": 3, "text": "т",
             "date": (now - timedelta(days=days_old)).isoformat()}
        db.insert_review_if_new(conn, "loc1", platform, r)
        rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
        conn.execute("UPDATE reviews SET sentiment=? WHERE id=?", (sentiment, rid))
        conn.commit()

    _review("p1", "2gis", "negative")
    _review("p2", "2gis", "positive")
    _review("p3", "yandex_maps", "positive")
    _review("p4", "2gis", "positive", days_old=60)  # вне периода — не в счёт

    since = (now - timedelta(days=7)).isoformat()
    result = db.get_platform_comparison_since(conn, "loc1", since)
    by_platform = {r["platform"]: r for r in result}

    assert by_platform["2gis"]["total"] == 2
    assert by_platform["2gis"]["negative"] == 1
    assert by_platform["2gis"]["negative_share_pct"] == 50
    assert by_platform["yandex_maps"]["total"] == 1
    assert by_platform["yandex_maps"]["negative_share_pct"] == 0


def test_hidden_problems_finds_high_rating_with_negative_tag(conn):
    """Sentiment vs Rating mismatch — 5★ отзыв с негативным тегом внутри не должен
    потеряться за общей высокой оценкой (см. PLAN.md, блок "Скрытые проблемы")."""
    now = datetime.now(timezone.utc)

    high_with_hidden_issue = {"external_id": "h1", "author": "Аня", "rating": 5, "text": "Отлично, но бассейн грязный",
                               "date": now.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", high_with_hidden_issue)
    hid = conn.execute("SELECT id FROM reviews WHERE external_review_id='h1'").fetchone()["id"]
    db.insert_review_tag(conn, hid, "чистота", "negative", "бассейн грязный")
    db.insert_review_tag(conn, hid, "тренеры", "positive", "отлично")

    high_clean = {"external_id": "h2", "author": "Боря", "rating": 5, "text": "Всё супер", "date": now.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", high_clean)
    hid2 = conn.execute("SELECT id FROM reviews WHERE external_review_id='h2'").fetchone()["id"]
    db.insert_review_tag(conn, hid2, "тренеры", "positive", "супер")  # нет негативных тегов — не должен попасть

    low_rating_negative = {"external_id": "h3", "author": "Вера", "rating": 2, "text": "Плохо", "date": now.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", low_rating_negative)
    hid3 = conn.execute("SELECT id FROM reviews WHERE external_review_id='h3'").fetchone()["id"]
    db.insert_review_tag(conn, hid3, "цена", "negative", "дорого")  # низкий рейтинг — это не "скрытая" проблема

    result = db.get_hidden_problems(conn, "loc1", min_rating=4)
    ids = {r["id"] for r in result}

    assert ids == {hid}


def test_reviews_paginated_filters_and_counts_total(conn):
    now = datetime.now(timezone.utc)

    def _review(external_id, platform, sentiment):
        r = {"external_id": external_id, "author": None, "rating": 3, "text": "т", "date": now.isoformat()}
        db.insert_review_if_new(conn, "loc1", platform, r)
        rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
        conn.execute("UPDATE reviews SET sentiment=? WHERE id=?", (sentiment, rid))
        conn.commit()

    _review("r1", "2gis", "negative")
    _review("r2", "2gis", "positive")
    _review("r3", "yandex_maps", "negative")

    page, total = db.get_reviews_paginated(conn, "loc1", platform="2gis")
    assert total == 2
    assert {r["external_review_id"] for r in page} == {"r1", "r2"}

    page, total = db.get_reviews_paginated(conn, "loc1", sentiment="negative")
    assert total == 2
    assert {r["external_review_id"] for r in page} == {"r1", "r3"}

    page, total = db.get_reviews_paginated(conn, "loc1", limit=1, offset=1)
    assert total == 3
    assert len(page) == 1


def test_reviews_for_tag_alert_matches_window_and_tag(conn):
    now = datetime.now(timezone.utc)

    def _review(external_id, days_old):
        r = {"external_id": external_id, "author": None, "rating": 2, "text": "т",
             "date": (now - timedelta(days=days_old)).isoformat()}
        db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
        rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
        conn.execute("UPDATE reviews SET sentiment='negative' WHERE id=?", (rid,))
        conn.commit()
        return rid

    in_window = _review("tw1", days_old=10)
    db.insert_review_tag(conn, in_window, "услуги", "negative", "плохо")

    out_of_window = _review("tw2", days_old=100)
    db.insert_review_tag(conn, out_of_window, "услуги", "negative", "плохо")

    other_tag = _review("tw3", days_old=5)
    db.insert_review_tag(conn, other_tag, "цена", "negative", "дорого")

    result = db.get_reviews_for_tag_alert(conn, "услуги", "loc1", window_days=30)
    ids = {r["id"] for r in result}

    assert ids == {in_window}


def test_reviews_for_repeat_offender_matches_author_platform_window(conn):
    now = datetime.now(timezone.utc)

    def _review(external_id, author, platform, days_old):
        r = {"external_id": external_id, "author": author, "rating": 1, "text": "плохо",
             "date": (now - timedelta(days=days_old)).isoformat()}
        db.insert_review_if_new(conn, "loc1", platform, r)
        rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
        conn.execute("UPDATE reviews SET sentiment='negative' WHERE id=?", (rid,))
        conn.commit()
        return rid

    match = _review("ro1", "Антон", "2gis", days_old=10)
    _review("ro2", "Антон", "yandex_maps", days_old=10)  # другая площадка — не в счёт
    _review("ro3", "Борис", "2gis", days_old=10)  # другой автор — не в счёт
    _review("ro4", "Антон", "2gis", days_old=100)  # вне окна — не в счёт

    result = db.get_reviews_for_repeat_offender(conn, "Антон", "2gis", "loc1", window_days=60)
    ids = {r["id"] for r in result}

    assert ids == {match}


def test_get_review_by_id_returns_none_for_missing(conn):
    assert db.get_review_by_id(conn, 9999) is None


def test_reviews_for_tag_alert_handles_mixed_date_formats(conn):
    """Регрессия (2026-07-14): drill-down показывал 13 отзывов вместо честных 11 из
    alert_engine — SQL-запрос сравнивал review_date >= ? КАК СТРОКИ, а площадки отдают
    разные форматы дат (Z-суффикс, +03:00, +07:00). Строковое сравнение даёт неверную
    границу окна на этих форматах. Тест намеренно НЕ использует единый .isoformat() для
    всех записей (как остальные тесты в файле) — иначе баг снова прошёл бы незамеченным."""
    now = datetime.now(timezone.utc)

    def _review_raw_date(external_id, review_date_str):
        r = {"external_id": external_id, "author": None, "rating": 2, "text": "т", "date": review_date_str}
        db.insert_review_if_new(conn, "loc1", "2gis", r)
        rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
        conn.execute("UPDATE reviews SET sentiment='negative' WHERE id=?", (rid,))
        conn.commit()
        return rid

    # 10 дней назад, но в формате с Z-суффиксом вместо +00:00 — строковое сравнение
    # "2026-07-04T10:00:00Z" >= "2026-06-14T10:00:00+00:00" даёт неверный результат
    # (буква 'Z' > '+' лексикографически, но это не значит "позже по времени")
    z_format_date = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    in_window = _review_raw_date("mixed1", z_format_date)
    db.insert_review_tag(conn, in_window, "услуги", "negative", "плохо")

    # 100 дней назад с offset +07:00 — вне 60-дневного окна, должен быть исключён
    old_date_offset = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S+07:00")
    out_of_window = _review_raw_date("mixed2", old_date_offset)
    db.insert_review_tag(conn, out_of_window, "услуги", "negative", "плохо")

    result = db.get_reviews_for_tag_alert(conn, "услуги", "loc1", window_days=60)
    ids = {r["id"] for r in result}

    assert ids == {in_window}
    assert out_of_window not in ids


def test_active_alerts_for_tags_matches_only_requested_tag_names(conn):
    """Регрессия (2026-07-15): main_reply.py передавал ВСЕ теги отзыва (включая
    позитивные) в get_active_alerts_for_tags — позитивный отзыв с тегом
    "персонал":positive получал в internal_note контекст открытого алерта по теме
    "персонал" (набранного другими, негативными отзывами), давая противоречивую
    заметку менеджеру ("3-я жалоба на персонал" на отзыве, где персонал хвалят).
    Сама функция БД работает корректно — ищет строго по переданным именам тегов,
    без знания о тональности; фикс был на стороне вызывающего кода (main_reply.py:
    передавать только теги с tag_sentiment='negative' этого конкретного отзыва)."""
    db.create_alert(conn, "персонал", "loc1", "yellow", 90, 3)
    db.create_alert(conn, "цена", "loc1", "red", 90, 5)

    result = db.get_active_alerts_for_tags(conn, "loc1", ["персонал"])
    assert {a["tag"] for a in result} == {"персонал"}

    result_empty = db.get_active_alerts_for_tags(conn, "loc1", [])
    assert result_empty == []

    result_none_match = db.get_active_alerts_for_tags(conn, "loc1", ["чистота"])
    assert result_none_match == []
