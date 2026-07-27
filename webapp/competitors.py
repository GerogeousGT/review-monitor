"""Конкурентная разведка (2026-07-27) — данные по конкурентам клиента живут вне
основной БД: это не алерты/SLA/ответы (см. PLAN.md — сознательно не встраивали в
основной пайплайн), а отдельные JSON-файлы clients/<slug>/competitors/<slug>.json,
собранные разовым скриптом (переиспользует collectors/yandex_maps.py и т.п.
напрямую, не main_collect.py). Не зависит от Flask — тот же принцип, что у
charts.py/period.py: должен оставаться тестируемым из корневого venv.

Формат файла:
{"name": "Black Fit", "url": "...", "platform": "yandex_maps", "collected_at": "2026-07-27",
 "reviews": [{"author":..., "rating":..., "text":..., "date":..., "sentiment":...,
              "tags": [{"tag":..., "s": "positive"|"neutral"|"negative"}, ...]}, ...]}
"""
import json
from pathlib import Path


def list_competitors(client_dir: Path) -> list[dict]:
    """[{"slug": "black_fit", "name": "Black Fit"}, ...] — сканирует competitors/*.json,
    ничего не нужно регистрировать в client_config.yaml. Битые/нечитаемые файлы
    тихо пропускаются, не роняют страницу."""
    comp_dir = client_dir / "competitors"
    if not comp_dir.is_dir():
        return []
    result = []
    for path in sorted(comp_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue  # чужой/промежуточный json в той же папке (например сырой список отзывов) — не наш формат
        result.append({"slug": path.stem, "name": data.get("name", path.stem)})
    return result


def load_competitor(client_dir: Path, competitor_slug: str) -> dict | None:
    path = client_dir / "competitors" / f"{competitor_slug}.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("reviews", [])
    return data


def aggregate_tags(reviews: list[dict]) -> list[dict]:
    """[{"tag":..., "total": N, "positive": N, "neutral": N, "negative": N}, ...],
    сортировка по total убыв. Без категорий (в отличие от
    core.db.get_tag_counts_by_category_since) — у конкурента нет своего словаря
    категорий, только плоский список тегов из общего словаря клиента."""
    totals: dict[str, dict] = {}
    for r in reviews:
        for t in r.get("tags", []):
            bucket = totals.setdefault(
                t["tag"], {"tag": t["tag"], "total": 0, "positive": 0, "neutral": 0, "negative": 0}
            )
            bucket["total"] += 1
            bucket[t["s"]] += 1
    return sorted(totals.values(), key=lambda b: -b["total"])


def sentiment_totals(reviews: list[dict]) -> dict:
    totals = {"positive": 0, "neutral": 0, "negative": 0}
    for r in reviews:
        s = r.get("sentiment")
        if s in totals:
            totals[s] += 1
    return totals
