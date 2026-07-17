"""Регрессия на баг #1 (2026-07-17): алерт-пинги не доходили в Telegram.

Причина: recompute_all и СЧИТАЕТ, и ЗАПИСЫВАЕТ состояние. В run_cycle.sh
main_alerts.py пересчитывал (применял diff), а main_notify.py секундой позже
пересчитывал ПОВТОРНО — состояние уже актуально, второй diff пустой, ни одно
сообщение об алерте не уходило. Фикс: отправка колокирована с единственным
пересчётом (main_alerts.send_alert_changes).

Тесты фиксируют два инварианта:
1. recompute_all идемпотентен (второй вызов подряд не видит изменений) — ИМЕННО
   поэтому отправку нельзя разносить с пересчётом на два разных скрипта.
2. send_alert_changes шлёт ровно одно сообщение на каждое изменение, без дублей,
   и различает open/update/resolve и тег/зону.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core import db
from agents.alert_engine import recompute_all
import main_alerts


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    db.ensure_location(c, "loc1", "Тестовый клуб", "Тюмень")
    yield c
    c.close()


CFG = {
    "alert_rules": {
        "default": [{"window_days": 30, "yellow_at": 3, "red_at": 5}],
        "overrides": {},
        "chronic_tiers": [],
    }
}


def _negative_review_with_tag(conn, external_id, tag, now, zone=None):
    r = {"external_id": external_id, "author": None, "rating": 1, "text": "т",
         "date": now.isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
    rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
    conn.execute("UPDATE reviews SET sentiment='negative' WHERE id=?", (rid,))
    db.insert_review_tag(conn, rid, tag, "negative", "плохо", zone=zone)
    conn.commit()
    return rid


def test_recompute_all_is_idempotent(conn):
    """Корень бага #1: второй подряд вызов recompute_all не возвращает изменений —
    первый уже применил их в БД. Поэтому нельзя, чтобы main_alerts пересчитывал, а
    main_notify пересчитывал ПОВТОРНО и слал: второму нечего слать."""
    now = datetime.now(timezone.utc)
    for i in range(3):
        _negative_review_with_tag(conn, f"r{i}", "персонал", now)

    first = recompute_all(conn, CFG, db)
    second = recompute_all(conn, CFG, db)

    assert len(first) == 1 and first[0]["action"] == "opened"
    assert second == []  # <-- ровно то, что раньше "съедало" алерт в main_notify


def test_send_alert_changes_one_message_per_change(conn):
    """send_alert_changes шлёт ровно одно сообщение на изменение, без дублей."""
    captured = []
    tag_changes = [
        {"action": "opened", "tag": "персонал", "location_id": "loc1",
         "severity": "yellow", "window_matched": 90, "count_in_window": 6},
        {"action": "updated", "tag": "цена", "location_id": "loc1", "previous_severity": "yellow",
         "severity": "red", "window_matched": 90, "count_in_window": 8},
    ]
    zone_changes = [
        {"action": "opened", "zone": "раздевалка", "location_id": "loc1",
         "severity": "yellow", "window_matched": 90, "count_in_window": 4},
    ]

    sent = main_alerts.send_alert_changes(conn, tag_changes, zone_changes, notify=captured.append)

    assert sent == 3
    assert len(captured) == 3
    assert "персонал" in captured[0]
    assert "цена" in captured[1]
    assert "раздевалка" in captured[2]
    # зональный пинг зовёт в дашборд, не даёт drill-down прямо в Telegram
    assert "дашборде" in captured[2]


def test_send_alert_changes_handles_resolved(conn):
    """resolved_auto использует "закрыт"/"в норме" форматтеры, не путается с open."""
    captured = []
    tag_changes = [{"action": "resolved_auto", "tag": "услуги", "location_id": "loc1",
                    "severity": "green", "window_matched": None, "count_in_window": 0}]
    zone_changes = [{"action": "resolved_auto", "zone": "бассейн", "location_id": "loc1",
                     "severity": "green", "window_matched": None, "count_in_window": 0}]

    main_alerts.send_alert_changes(conn, tag_changes, zone_changes, notify=captured.append)

    assert "закрыт" in captured[0] and "услуги" in captured[0]
    assert "в норме" in captured[1] and "бассейн" in captured[1]


def test_send_alert_changes_survives_send_failure(conn):
    """Сбой доставки одного сообщения не должен ронять весь цикл (set -e в run_cycle.sh)
    и не мешает отправке остальных."""
    calls = []

    def flaky(text):
        calls.append(text)
        if "первый" in text:
            raise RuntimeError("Telegram 500")

    tag_changes = [
        {"action": "opened", "tag": "первый", "location_id": "loc1",
         "severity": "yellow", "window_matched": 90, "count_in_window": 6},
        {"action": "opened", "tag": "второй", "location_id": "loc1",
         "severity": "yellow", "window_matched": 90, "count_in_window": 6},
    ]

    sent = main_alerts.send_alert_changes(conn, tag_changes, [], notify=flaky)

    assert len(calls) == 2  # попытка была на оба
    assert sent == 1        # засчитан только успешный
