"""Alert Engine — чистые функции, без БД и LLM. Тестируем формулы и граничные случаи."""
from datetime import datetime, timedelta, timezone

from agents.alert_engine import (
    compute_burst_severity,
    compute_chronic_severity,
    compute_tag_severity,
)

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)

BURST_RULES = {
    "default": [
        {"window_days": 30, "yellow_at": 3, "red_at": 5},
        {"window_days": 90, "yellow_at": 3, "red_at": 6},
    ],
    "overrides": {},
}

CHRONIC_TIERS = [
    {"window_days": 180, "threshold": 6},
    {"window_days": 360, "threshold": 9},
    {"window_days": 720, "threshold": 14},
]


def _events(n: int, days_ago: int = 0) -> list[dict]:
    date = (NOW - timedelta(days=days_ago)).isoformat()
    return [{"review_id": 1000 + i, "review_date": date} for i in range(n)]


def test_burst_below_threshold_is_green():
    result = compute_burst_severity(_events(2), "тег", BURST_RULES, NOW)
    assert result["severity"] == "green"


def test_burst_yellow_and_red_thresholds():
    assert compute_burst_severity(_events(3), "тег", BURST_RULES, NOW)["severity"] == "yellow"
    assert compute_burst_severity(_events(5), "тег", BURST_RULES, NOW)["severity"] == "red"


def test_burst_dedups_same_review_id():
    """Один отзыв с двумя упоминаниями темы не должен считаться дважды."""
    events = _events(2) + [{"review_id": 1000, "review_date": NOW.isoformat()}]  # дубль id=1000
    result = compute_burst_severity(events, "тег", BURST_RULES, NOW)
    assert result["count_in_window"] == 2


def test_chronic_stepped_escalation():
    assert compute_chronic_severity(_events(5), CHRONIC_TIERS, NOW)["severity"] == "green"
    assert compute_chronic_severity(_events(6), CHRONIC_TIERS, NOW)["severity"] == "yellow"

    red_360 = compute_chronic_severity(_events(9), CHRONIC_TIERS, NOW)
    assert red_360["severity"] == "red"
    assert red_360["window_matched"] == 360

    red_720 = compute_chronic_severity(_events(14), CHRONIC_TIERS, NOW)
    assert red_720["window_matched"] == 720


def test_chronic_requires_sequential_trigger():
    """Старый всплеск за пределами 180 дней не должен ложно засветиться как хроника
    в окне 360/720, если сейчас (в первом окне) уже тихо."""
    old_events = _events(20, days_ago=300)  # 20 жалоб, но все старше 180 дней
    result = compute_chronic_severity(old_events, CHRONIC_TIERS, NOW)
    assert result["severity"] == "green"


def test_tag_severity_takes_worse_of_burst_and_chronic():
    """"услуги": 5 за 90 дней даёт burst=green (порог 6), но 11 за 180 дней даёт
    chronic=yellow — итоговая severity должна быть yellow, не green."""
    cfg = {**BURST_RULES, "chronic_tiers": CHRONIC_TIERS}
    events = _events(6, days_ago=150)  # видны в 90-дневном окне? нет — только в 180
    result = compute_tag_severity(events, "услуги", cfg, NOW)
    assert result["severity"] == "yellow"
