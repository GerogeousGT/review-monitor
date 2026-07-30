"""Конкурентная разведка (2026-07-27) — данные по конкурентам клиента живут вне
основной БД: это не алерты/SLA/ответы (см. PLAN.md — сознательно не встраивали в
основной пайплайн), а отдельные JSON-файлы clients/<slug>/competitors/<slug>.json,
собранные разовым скриптом (переиспользует collectors/yandex_maps.py и т.п.
напрямую, не main_collect.py). Не зависит от Flask — тот же принцип, что у
charts.py/period.py: должен оставаться тестируемым из корневого venv.

Формат файла (v2, 2026-07-30 — один конкурент может быть собран сразу с
нескольких площадок, отзывы сливаются в один список, каждый несёт свой platform):
{"name": "Фитберри", "sources": [{"platform": "yandex_maps", "url": "...", "collected_at": "..."}, ...],
 "reviews": [{"platform":..., "author":..., "rating":..., "text":..., "date":..., "sentiment":...,
              "reply_status": "replied"|"pending", "reply_text": ...,
              "tags": [{"tag":..., "s": "positive"|"neutral"|"negative"}, ...]}, ...],
 "synthesis": {"praised": [...], "complaints": [...], "named_staff": [...],
               "reputation": {"patterns": [...], "verdict": "..."}, "verdict": "..."}}

Старый формат (v1, один конкурент = одна площадка, поля platform/url/collected_at
на верхнем уровне) читается как есть — load_competitor() приводит его к sources
на лету, чтобы не переписывать уже собранные файлы (Black Fit, Fitness Life, Profi)."""
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
    data.setdefault("synthesis", None)
    if "sources" not in data and data.get("platform"):
        data["sources"] = [{
            "platform": data.get("platform"),
            "url": data.get("url"),
            "collected_at": data.get("collected_at"),
        }]
    data.setdefault("sources", [])
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


def reply_stats(reviews: list[dict]) -> dict:
    """Частота ответов клуба на отзывы — считается кодом (не на глаз при
    ручном разборе), чтобы % не разъезжался при добавлении новых отзывов.
    reply_status по умолчанию "pending", если поле вообще отсутствует
    (старые файлы, собранные до 2026-07-30, где reply_text/статус не было)."""
    total = len(reviews)
    replied = sum(1 for r in reviews if r.get("reply_status") == "replied")
    pct = round(replied / total * 100) if total else 0
    return {"total": total, "replied": replied, "pending": total - replied, "pct": pct}
