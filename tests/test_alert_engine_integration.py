"""recompute_all/recompute_repeat_offenders требуют реальную БД (не чистые функции,
в отличие от test_alert_engine.py) — интеграционные тесты на изолированной SQLite."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core import db
from agents.alert_engine import recompute_all


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


def _negative_review_with_tag(conn, external_id, tag, days_old, now):
    r = {"external_id": external_id, "author": None, "rating": 1, "text": "т",
         "date": (now - timedelta(days=days_old)).isoformat()}
    db.insert_review_if_new(conn, "loc1", "yandex_maps", r)
    rid = conn.execute("SELECT id FROM reviews WHERE external_review_id=?", (external_id,)).fetchone()["id"]
    conn.execute("UPDATE reviews SET sentiment='negative' WHERE id=?", (rid,))
    db.insert_review_tag(conn, rid, tag, "negative", "плохо")
    conn.commit()
    return rid


def test_recompute_all_resolves_orphaned_alert_when_tag_renamed(conn):
    """Регрессия (2026-07-14): алерт по тегу, который переименован/удалён из словаря
    и больше не встречается в текущих событиях, раньше оставался status='open' навсегда
    ("осиротевший" алерт) — recompute_all строил цикл ИЗ events, никогда не проверяя
    уже существующие алерты по тегам, которых в events больше нет. Найдено на реальных
    данных: тег "услуги" разбили на "возврат средств"+"информирование", старый алерт
    id=2 (red, count=11) провис в списке активных даже после пересчёта."""
    now = datetime.now(timezone.utc)

    for i in range(5):
        _negative_review_with_tag(conn, f"old{i}", "услуги", days_old=10, now=now)

    recompute_all(conn, CFG, db)
    old_alert = db.get_active_alert(conn, "услуги", "loc1")
    assert old_alert is not None
    assert old_alert["severity"] == "red"

    # "Перетегировка" — как в реальном сценарии: тег "услуги" больше не используется
    conn.execute("UPDATE review_tags SET tag='новый_тег' WHERE tag='услуги'")
    conn.commit()

    changes = recompute_all(conn, CFG, db)

    assert db.get_active_alert(conn, "услуги", "loc1") is None  # закрыт, не висит "призраком"
    resolved_changes = [c for c in changes if c["tag"] == "услуги" and c["action"] == "resolved_auto"]
    assert len(resolved_changes) == 1
