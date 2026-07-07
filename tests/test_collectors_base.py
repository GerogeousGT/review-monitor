from collectors.base import synthetic_id


def test_synthetic_id_is_deterministic():
    """Один и тот же отзыв должен давать один и тот же ID между запусками —
    иначе дедупликация по external_id (INSERT OR IGNORE) сломается."""
    id1 = synthetic_id("Аня", "2026-01-01", "Отличный клуб")
    id2 = synthetic_id("Аня", "2026-01-01", "Отличный клуб")
    assert id1 == id2


def test_synthetic_id_differs_for_different_input():
    id1 = synthetic_id("Аня", "2026-01-01", "Отличный клуб")
    id2 = synthetic_id("Боря", "2026-01-01", "Отличный клуб")
    assert id1 != id2


def test_synthetic_id_handles_none_parts():
    # author может быть None (анонимный отзыв) — не должно падать
    result = synthetic_id(None, "2026-01-01", "текст")
    assert isinstance(result, str) and len(result) == 40  # sha1 hex digest
