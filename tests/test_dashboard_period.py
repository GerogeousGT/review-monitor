"""webapp/period.py — resolve_period/auto_granularity (свой диапазон дат,
2026-07-17, запрос Жоржа). period.py — чистый stdlib-модуль (никакого Flask),
поэтому тестируется из корневого venv через прямую вставку webapp/ в sys.path,
как charts.py в test_charts.py."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
sys.path.insert(0, str(WEBAPP_DIR))

import period as period_mod  # noqa: E402


def test_auto_granularity_short_range_is_day():
    now = datetime.now(timezone.utc)
    assert period_mod.auto_granularity(now - timedelta(days=10), now) == "day"


def test_auto_granularity_medium_range_is_week():
    now = datetime.now(timezone.utc)
    assert period_mod.auto_granularity(now - timedelta(days=90), now) == "week"


def test_auto_granularity_long_range_is_month():
    now = datetime.now(timezone.utc)
    assert period_mod.auto_granularity(now - timedelta(days=400), now) == "month"


def test_preset_week():
    since, until, granularity, key = period_mod.resolve_period("week")
    assert key == "week"
    assert granularity == "day"


def test_preset_quarter():
    since, until, granularity, key = period_mod.resolve_period("quarter")
    assert key == "quarter"
    assert granularity == "week"


def test_invalid_preset_falls_back_to_default():
    since, until, granularity, key = period_mod.resolve_period("garbage")
    assert key == period_mod.DEFAULT_PERIOD


def test_custom_range_valid_dates():
    since, until, granularity, key = period_mod.resolve_period("custom", "2026-01-01", "2026-01-10")
    assert key == "custom"
    assert since.startswith("2026-01-01")
    assert until.startswith("2026-01-10")
    assert granularity == "day"  # 9-дневный диапазон


def test_custom_range_missing_dates_falls_back():
    """period=custom, но без since/until — тихий откат, effective_key != 'custom',
    чтобы шаблон не подсветил вкладку 'Свой диапазон' на дефолтных данных."""
    since, until, granularity, key = period_mod.resolve_period("custom", "", "")
    assert key == period_mod.DEFAULT_PERIOD


def test_custom_range_malformed_date_falls_back():
    since, until, granularity, key = period_mod.resolve_period("custom", "not-a-date", "2026-01-10")
    assert key == period_mod.DEFAULT_PERIOD


def test_custom_range_since_after_until_is_swapped_not_error():
    since, until, granularity, key = period_mod.resolve_period("custom", "2026-01-10", "2026-01-01")
    assert key == "custom"
    since_dt = datetime.fromisoformat(since)
    until_dt = datetime.fromisoformat(until)
    assert since_dt < until_dt  # переставлены местами, не ошибка


def test_custom_range_until_in_future_clamped_to_now():
    future = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%d")
    since, until, granularity, key = period_mod.resolve_period("custom", "2026-01-01", future)
    until_dt = datetime.fromisoformat(until)
    assert until_dt <= datetime.now(timezone.utc) + timedelta(seconds=5)  # запас на выполнение теста


def test_custom_range_too_wide_is_capped():
    since, until, granularity, key = period_mod.resolve_period("custom", "2000-01-01", "2026-07-17")
    since_dt = datetime.fromisoformat(since)
    until_dt = datetime.fromisoformat(until)
    assert (until_dt - since_dt).days <= period_mod.MAX_CUSTOM_RANGE_DAYS + 1  # допуск на включительный until
    assert granularity == "month"  # широкий диапазон даже после каппинга (2 года)
