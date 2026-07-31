"""webapp/competitors.py — конкурентная разведка (2026-07-27), JSON-файлы вне
основной БД. Чистый stdlib-модуль (никакого Flask), тестируется из корневого
venv тем же паттерном, что period.py в test_dashboard_period.py."""
import json
import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
sys.path.insert(0, str(WEBAPP_DIR))

import competitors  # noqa: E402


def _write_competitor(tmp_path: Path, slug: str, name: str, reviews: list[dict]) -> None:
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir(exist_ok=True)
    doc = {"name": name, "url": "https://example.com", "platform": "yandex_maps", "reviews": reviews}
    with (comp_dir / f"{slug}.json").open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


def test_list_competitors_empty_when_no_dir(tmp_path):
    assert competitors.list_competitors(tmp_path) == []


def test_list_competitors_finds_all_json_files(tmp_path):
    _write_competitor(tmp_path, "black_fit", "Black Fit", [])
    _write_competitor(tmp_path, "sparta", "Sparta", [])
    result = competitors.list_competitors(tmp_path)
    assert {c["slug"] for c in result} == {"black_fit", "sparta"}
    assert {c["name"] for c in result} == {"Black Fit", "Sparta"}


def test_list_competitors_skips_broken_json(tmp_path):
    _write_competitor(tmp_path, "good", "Good Gym", [])
    comp_dir = tmp_path / "competitors"
    (comp_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
    result = competitors.list_competitors(tmp_path)
    assert [c["slug"] for c in result] == ["good"]


def test_list_competitors_skips_valid_json_wrong_shape(tmp_path):
    """Регрессия (2026-07-27): в той же папке оказались промежуточные файлы
    сбора (сырой список отзывов, не {"name":...,"reviews":...}) — валидный
    JSON, но список, а не словарь. .get() на списке падает с AttributeError,
    роняя всю страницу "Конкуренты" — найдено на реальном проде до деплоя."""
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    (comp_dir / "raw_scrape.json").write_text('[{"author": "А", "text": "..."}]', encoding="utf-8")
    _write_competitor(tmp_path, "good", "Good Gym", [])
    result = competitors.list_competitors(tmp_path)
    assert [c["slug"] for c in result] == ["good"]


def test_list_competitors_skips_underscore_prefixed_files(tmp_path):
    """_market_synthesis.json — служебный файл, не отдельный конкурент."""
    _write_competitor(tmp_path, "good", "Good Gym", [])
    comp_dir = tmp_path / "competitors"
    (comp_dir / "_market_synthesis.json").write_text('{"opportunities": []}', encoding="utf-8")
    result = competitors.list_competitors(tmp_path)
    assert [c["slug"] for c in result] == ["good"]


def test_load_market_synthesis_returns_none_when_missing(tmp_path):
    assert competitors.load_market_synthesis(tmp_path) is None


def test_load_market_synthesis_returns_document(tmp_path):
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    doc = {"opportunities": [{"title": "X", "note": "Y", "evidence": []}], "verdict": "Z"}
    (comp_dir / "_market_synthesis.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    result = competitors.load_market_synthesis(tmp_path)
    assert result["opportunities"][0]["title"] == "X"
    assert result["verdict"] == "Z"


def test_load_market_synthesis_returns_none_for_wrong_shape(tmp_path):
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    (comp_dir / "_market_synthesis.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert competitors.load_market_synthesis(tmp_path) is None


def test_load_competitor_returns_none_when_missing(tmp_path):
    assert competitors.load_competitor(tmp_path, "nope") is None


def test_load_competitor_returns_full_document(tmp_path):
    _write_competitor(tmp_path, "black_fit", "Black Fit", [{"author": "А", "rating": 5}])
    doc = competitors.load_competitor(tmp_path, "black_fit")
    assert doc["name"] == "Black Fit"
    assert doc["reviews"] == [{"author": "А", "rating": 5}]


def test_load_competitor_returns_none_for_wrong_shape(tmp_path):
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    (comp_dir / "raw_scrape.json").write_text('[{"author": "А"}]', encoding="utf-8")
    assert competitors.load_competitor(tmp_path, "raw_scrape") is None


def test_load_competitor_defaults_missing_reviews_to_empty_list(tmp_path):
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    (comp_dir / "no_reviews.json").write_text('{"name": "X"}', encoding="utf-8")
    doc = competitors.load_competitor(tmp_path, "no_reviews")
    assert doc["reviews"] == []


def test_load_competitor_defaults_missing_synthesis_to_none(tmp_path):
    """Независимый качественный разбор (темы/цитаты) — необязательное поле,
    не у каждого конкурента он есть с самого начала. Шаблон должен спокойно
    скрыть секцию, если synthesis отсутствует."""
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    (comp_dir / "no_synthesis.json").write_text('{"name": "X", "reviews": []}', encoding="utf-8")
    doc = competitors.load_competitor(tmp_path, "no_synthesis")
    assert doc["synthesis"] is None


def test_aggregate_tags_sums_by_sentiment():
    reviews = [
        {"tags": [{"tag": "тренеры", "s": "positive"}, {"tag": "душевые", "s": "negative"}]},
        {"tags": [{"tag": "тренеры", "s": "positive"}]},
        {"tags": [{"tag": "душевые", "s": "negative"}, {"tag": "душевые", "s": "negative"}]},
    ]
    result = competitors.aggregate_tags(reviews)
    by_tag = {r["tag"]: r for r in result}
    assert by_tag["тренеры"] == {"tag": "тренеры", "total": 2, "positive": 2, "neutral": 0, "negative": 0}
    assert by_tag["душевые"] == {"tag": "душевые", "total": 3, "positive": 0, "neutral": 0, "negative": 3}
    # сортировка по total убыв.
    assert result[0]["tag"] == "душевые"


def test_aggregate_tags_empty_reviews_is_empty():
    assert competitors.aggregate_tags([]) == []


def test_sentiment_totals_counts_each_bucket():
    reviews = [{"sentiment": "positive"}, {"sentiment": "positive"}, {"sentiment": "negative"}, {"sentiment": "neutral"}]
    assert competitors.sentiment_totals(reviews) == {"positive": 2, "neutral": 1, "negative": 1}


def test_sentiment_totals_ignores_unknown_values():
    reviews = [{"sentiment": "positive"}, {"sentiment": "weird"}, {}]
    assert competitors.sentiment_totals(reviews) == {"positive": 1, "neutral": 0, "negative": 0}


def test_load_competitor_converts_v1_platform_to_sources(tmp_path):
    """Старые файлы (Black Fit, Fitness Life, Profi — собраны до 2026-07-30)
    хранят platform/url/collected_at на верхнем уровне, не в sources[] —
    должны читаться как есть, без ручной миграции файлов."""
    _write_competitor(tmp_path, "black_fit", "Black Fit", [])
    doc = competitors.load_competitor(tmp_path, "black_fit")
    assert doc["sources"] == [{"platform": "yandex_maps", "url": "https://example.com", "collected_at": None}]


def test_load_competitor_v2_sources_passthrough(tmp_path):
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    doc_in = {
        "name": "Фитберри",
        "sources": [
            {"platform": "yandex_maps", "url": "https://example.com/ya", "collected_at": "2026-07-30"},
            {"platform": "2gis", "url": "https://example.com/2gis", "collected_at": "2026-07-30"},
        ],
        "reviews": [],
    }
    (comp_dir / "fitberri.json").write_text(json.dumps(doc_in, ensure_ascii=False), encoding="utf-8")
    doc = competitors.load_competitor(tmp_path, "fitberri")
    assert doc["sources"] == doc_in["sources"]


def test_load_competitor_defaults_missing_sources_to_empty_list(tmp_path):
    comp_dir = tmp_path / "competitors"
    comp_dir.mkdir()
    (comp_dir / "no_sources.json").write_text('{"name": "X", "reviews": []}', encoding="utf-8")
    doc = competitors.load_competitor(tmp_path, "no_sources")
    assert doc["sources"] == []


def test_reply_stats_counts_replied_and_pending():
    reviews = [
        {"reply_status": "replied"},
        {"reply_status": "replied"},
        {"reply_status": "pending"},
    ]
    assert competitors.reply_stats(reviews) == {"total": 3, "replied": 2, "pending": 1, "pct": 67}


def test_reply_stats_empty_reviews_is_zero():
    assert competitors.reply_stats([]) == {"total": 0, "replied": 0, "pending": 0, "pct": 0}


def test_reply_stats_missing_reply_status_counts_as_pending():
    """Старые файлы, собранные до 2026-07-30, вообще не имеют reply_status —
    не должны считаться "отвеченными" по умолчанию."""
    reviews = [{"author": "А"}, {"reply_status": "replied"}]
    assert competitors.reply_stats(reviews) == {"total": 2, "replied": 1, "pending": 1, "pct": 50}
