"""Регрессия на баг #4/#5 (2026-07-17): main_reanalyze.py дублировал логику записи
результата анализа с расхождением от main_analyze.py — не нормализовал zone и не
сохранял evidence/review_id для новых pending-тегов. Найдено на реальных данных
прода daudelsport ("женская раздевалка", "бар" — ненормализованные зоны).

Исправлено выносом общей persist_analysis()/normalize_zone() в main_analyze.py,
main_reanalyze.py теперь импортирует их оттуда — эти тесты покрывают общий путь,
который используют оба скрипта."""
import sqlite3

import pytest

from core import db
from main_analyze import persist_analysis, normalize_zone


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    db.ensure_location(c, "loc1", "Тестовый клуб", "Тюмень")
    r = {"external_id": "ext1", "author": "Автор", "rating": 3, "text": "т", "date": "2026-07-01T00:00:00+00:00"}
    db.insert_review_if_new(c, "loc1", "yandex_maps", r)
    yield c
    c.close()


def _review_id(conn):
    return conn.execute("SELECT id FROM reviews WHERE external_review_id='ext1'").fetchone()["id"]


def test_normalize_zone_exact_match():
    assert normalize_zone("раздевалка", {"раздевалка", "бассейн"}) == "раздевалка"


def test_normalize_zone_substring_match():
    """Основной баг-кейс: "женская раздевалка" (свободный текст модели) должна
    схлопнуться в известную зону "раздевалка", иначе зональный алерт дробится."""
    assert normalize_zone("женская раздевалка", {"раздевалка", "бассейн"}) == "раздевалка"


def test_normalize_zone_no_match_kept_as_is():
    assert normalize_zone("совсем новое место", {"раздевалка", "бассейн"}) == "совсем новое место"


def test_normalize_zone_none():
    assert normalize_zone(None, {"раздевалка"}) is None


def test_persist_analysis_normalizes_zone(conn):
    review_id = _review_id(conn)
    result = {
        "sentiment": "negative", "sentiment_score": 7, "sentiment_reasoning": "жалоба", "urgency": False,
        "aspects": [
            {"tag": "чистота", "tag_sentiment": "negative", "tag_evidence": "грязно",
             "is_new": False, "category": None, "zone": "Женская Раздевалка"},
        ],
    }
    persist_analysis(
        conn, review_id, "2026-07-01T00:00:00+00:00", result, {"negative": 24},
        active_tags={"чистота"}, known_categories={"зона клуба"}, known_zones={"раздевалка"},
    )
    tags = db.get_review_tags(conn, review_id)
    assert tags[0]["zone"] == "раздевалка"  # не "женская раздевалка" — схлопнуто


def test_persist_analysis_saves_evidence_for_pending_tag(conn):
    """До фикса: main_reanalyze.py вызывал insert_tag_if_new без evidence/review_id —
    approval-карточка в дашборде показывала бы "отзыв не найден, создан до этой версии"
    даже для только что созданного pending-тега."""
    review_id = _review_id(conn)
    result = {
        "sentiment": "negative", "sentiment_score": 6, "sentiment_reasoning": "жалоба", "urgency": False,
        "aspects": [
            {"tag": "новая_тема", "tag_sentiment": "negative", "tag_evidence": "конкретная цитата",
             "is_new": True, "category": "сервис", "zone": None},
        ],
    }
    persist_analysis(
        conn, review_id, "2026-07-01T00:00:00+00:00", result, {"negative": 24},
        active_tags=set(), known_categories={"сервис"}, known_zones=set(),
    )
    pending = db.get_pending_tags(conn)
    assert len(pending) == 1
    assert pending[0]["tag"] == "новая_тема"
    assert pending[0]["pending_evidence"] == "конкретная цитата"
    assert pending[0]["pending_review_id"] == review_id


def test_persist_analysis_returns_tags_summary_string(conn):
    review_id = _review_id(conn)
    result = {
        "sentiment": "positive", "sentiment_score": 8, "sentiment_reasoning": "похвала", "urgency": False,
        "aspects": [{"tag": "тренеры", "tag_sentiment": "positive", "tag_evidence": "супер", "is_new": False, "category": None, "zone": None}],
    }
    summary = persist_analysis(
        conn, review_id, "2026-07-01T00:00:00+00:00", result, {"positive": None},
        active_tags={"тренеры"}, known_categories=set(), known_zones=set(),
    )
    assert summary == "тренеры:positive"


def test_persist_analysis_empty_aspects_returns_placeholder(conn):
    review_id = _review_id(conn)
    result = {"sentiment": "neutral", "sentiment_score": 5, "sentiment_reasoning": "нейтрально", "urgency": False, "aspects": []}
    summary = persist_analysis(
        conn, review_id, "2026-07-01T00:00:00+00:00", result, {"neutral": 72},
        active_tags=set(), known_categories=set(), known_zones=set(),
    )
    assert summary == "без тем"
