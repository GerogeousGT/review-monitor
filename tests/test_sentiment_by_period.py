"""get_review_sentiment_counts_by_period — агрегация тональности по периодам для
графика динамики (см. PLAN.md "Дашборд клиента v2", запрос Жоржа 2026-07-14:
график вместо статичного статус-бара за фиксированные 30 дней)."""
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


def _review(conn, external_id, sentiment, dt):
    r = {"external_id": external_id, "author": None, "rating": 3, "text": "т", "date": dt.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
    conn.execute("UPDATE reviews SET sentiment=? WHERE external_review_id=?", (sentiment, external_id))
    conn.commit()


def test_groups_by_day(conn):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    _review(conn, "r1", "positive", now)
    _review(conn, "r2", "negative", now)
    _review(conn, "r3", "positive", now - timedelta(days=1))

    result = db.get_review_sentiment_counts_by_period(
        conn, "loc1", (now - timedelta(days=1)).isoformat(), now.isoformat(), "day"
    )

    by_period = {r["period"]: r for r in result}
    assert by_period["2026-07-15"]["positive"] == 1
    assert by_period["2026-07-15"]["negative"] == 1
    assert by_period["2026-07-14"]["positive"] == 1


def test_groups_by_week(conn):
    """Два отзыва в одной ISO-неделе схлопываются в один bucket."""
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)  # среда, ISO week 29
    _review(conn, "r1", "negative", now)
    _review(conn, "r2", "negative", now - timedelta(days=2))  # понедельник, та же неделя

    result = db.get_review_sentiment_counts_by_period(
        conn, "loc1", (now - timedelta(days=7)).isoformat(), now.isoformat(), "week"
    )
    week_buckets = [r for r in result if r["negative"] > 0]
    assert len(week_buckets) == 1
    assert week_buckets[0]["negative"] == 2


def test_groups_by_month(conn):
    d1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    d2 = datetime(2026, 7, 28, tzinfo=timezone.utc)
    _review(conn, "r1", "positive", d1)
    _review(conn, "r2", "positive", d2)

    result = db.get_review_sentiment_counts_by_period(conn, "loc1", d1.isoformat(), d2.isoformat(), "month")
    assert len(result) == 1
    assert result[0]["period"] == "2026-07"
    assert result[0]["positive"] == 2


def test_empty_periods_are_zero_filled_not_dropped(conn):
    """Диапазон без единого отзыва посреди периода не должен выпадать из графика —
    иначе ось X визуально рвётся."""
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    _review(conn, "r1", "positive", now - timedelta(days=4))
    _review(conn, "r2", "positive", now)

    result = db.get_review_sentiment_counts_by_period(
        conn, "loc1", (now - timedelta(days=4)).isoformat(), now.isoformat(), "day"
    )
    assert len(result) == 5  # 5 календарных дней, даже если 3 из них пустые
    empty_days = [r for r in result if r["positive"] == 0 and r["negative"] == 0 and r["neutral"] == 0]
    assert len(empty_days) == 3


def test_february_month_boundary_not_skipped(conn):
    """Регрессия на фиксированный шаг timedelta(days=31): короткий месяц
    (февраль) не должен пропускаться при дневном шаге заполнения пустых периодов."""
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)
    until = datetime(2026, 3, 15, tzinfo=timezone.utc)
    result = db.get_review_sentiment_counts_by_period(conn, "loc1", since.isoformat(), until.isoformat(), "month")
    periods = {r["period"] for r in result}
    assert periods == {"2026-01", "2026-02", "2026-03"}


def test_results_sorted_chronologically(conn):
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    _review(conn, "r1", "negative", now)
    _review(conn, "r2", "negative", now - timedelta(days=10))

    result = db.get_review_sentiment_counts_by_period(
        conn, "loc1", (now - timedelta(days=10)).isoformat(), now.isoformat(), "day"
    )
    periods = [r["period"] for r in result]
    assert periods == sorted(periods)


def test_invalid_granularity_raises(conn):
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        db.get_review_sentiment_counts_by_period(conn, "loc1", now.isoformat(), now.isoformat(), "year")


def test_out_of_range_reviews_excluded(conn):
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    _review(conn, "r1", "negative", now - timedelta(days=100))  # далеко до since
    _review(conn, "r2", "positive", now)

    result = db.get_review_sentiment_counts_by_period(
        conn, "loc1", (now - timedelta(days=5)).isoformat(), now.isoformat(), "day"
    )
    total_negative = sum(r["negative"] for r in result)
    assert total_negative == 0  # старый отзыв вне окна не попал
