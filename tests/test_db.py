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


def test_top_tags_since_filters_by_location_and_period(conn):
    """Регрессия-подобный тест: топ тегов должен учитывать только эту локацию и только
    отзывы внутри периода (review_date), а не всю историю всех точек."""
    now = datetime.now(timezone.utc)
    db.ensure_location(conn, "loc2", "Другой клуб", "Москва")

    def _review(external_id, days_old, loc="loc1"):
        r = {"external_id": external_id, "author": None, "rating": 4, "text": "т",
             "date": (now - timedelta(days=days_old)).isoformat()}
        db.insert_review_if_new(conn, loc, "yandex_maps", r)
        return conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]

    fresh1 = _review("t1", days_old=1)
    fresh2 = _review("t2", days_old=2)
    old = _review("t3", days_old=30)  # старше 7-дневного окна
    other_loc = _review("t4", days_old=1, loc="loc2")  # другая локация

    db.insert_review_tag(conn, fresh1, "бассейн", "positive", "evidence")
    db.insert_review_tag(conn, fresh2, "бассейн", "positive", "evidence")
    db.insert_review_tag(conn, old, "бассейн", "positive", "evidence")  # вне окна — не в счёт
    db.insert_review_tag(conn, other_loc, "бассейн", "positive", "evidence")  # чужая точка — не в счёт

    since = (now - timedelta(days=7)).isoformat()
    top = db.get_top_tags_since(conn, "loc1", since, limit=3)

    assert top == [{"tag": "бассейн", "count": 2}]


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
