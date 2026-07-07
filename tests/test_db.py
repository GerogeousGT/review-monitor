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
    counts = db.get_review_sentiment_counts_since(conn, since)
    assert counts.get("positive", 0) == 1  # только fresh_review, старый не в счёт
