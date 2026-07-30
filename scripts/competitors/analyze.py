"""Разовый структурный тег-анализ отзывов конкурента — переиспользует РЕАЛЬНЫЙ
словарь тегов клиента и agents/sentiment_analyst.py (тот же промпт, что видит
продукт на своих отзывах), но НЕ пишет ничего в reviews.db клиента — конкурент
не клиент, БД клиента только читается (см. PLAN.md "Конкурентная разведка" про
изоляцию — конкурентные данные никогда не попадают в основной пайплайн).

Собирает готовый clients/<slug>/competitors/<comp>.json из manifest-файла
(сырые JSON от collect.py по каждой площадке + её метаданные). Тег/тональность
на каждый отзыв — здесь; "synthesis" (качественный разбор, цитаты, вывод про
работу с репутацией) дописывается вручную ПОСЛЕ, отдельным шагом — это суждение,
не то, что скрипт может сделать за нас.

Manifest (JSON), собирается вручную под каждого конкурента:
{"name": "Фитберри",
 "sources": [{"platform": "yandex_maps", "url": "...", "collected_at": "2026-07-30", "raw": "/tmp/fitberri_yandex.json"},
             {"platform": "2gis", "url": "...", "collected_at": "2026-07-30", "raw": "/tmp/fitberri_2gis.json"}]}

Использование (нужен CLIENT_SLUG в env — им же грузится LLM-провайдер, core/llm_provider.py):
  CLIENT_SLUG=daudelsport python scripts/competitors/analyze.py --client daudelsport --manifest /tmp/fitberri_manifest.json --out clients/daudelsport/competitors/fitberri.json
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import db as core_db  # noqa: E402
from agents.sentiment_analyst import analyze_review  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True, help="slug клиента — читаем ЕГО словарь тегов (read-only)")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    db_path = PROJECT_ROOT / "clients" / args.client / "db" / "reviews.db"
    conn = core_db.get_connection(db_path=db_path)
    core_db.init_db(conn)
    tag_dictionary = core_db.get_tag_dictionary(conn, active_only=True)
    category_dictionary = core_db.get_category_dictionary(conn)
    active_tags = {t["tag"] for t in tag_dictionary}
    conn.close()

    all_reviews = []
    sources = []
    for src in manifest["sources"]:
        raw = json.loads(Path(src["raw"]).read_text(encoding="utf-8"))
        sources.append({"platform": src["platform"], "url": src.get("url"), "collected_at": src.get("collected_at")})
        for r in raw:
            try:
                result = analyze_review(r.get("text") or "", r.get("rating"), tag_dictionary, category_dictionary)
                r["sentiment"] = result["sentiment"]
                # Только теги ИЗ активного словаря клиента — is_new-предложения модели тут
                # не проходят approval-флоу (его для конкурента просто нет), отбрасываем их,
                # а не тянем в счётчик тем, которых нет в словаре.
                r["tags"] = [
                    {"tag": a["tag"], "s": a["tag_sentiment"]}
                    for a in result.get("aspects", [])
                    if a.get("tag") in active_tags
                ]
            except Exception as e:
                print(f"[{src['platform']}] пропуск отзыва (ошибка анализа): {e}")
                r.setdefault("sentiment", None)
                r.setdefault("tags", [])
            all_reviews.append(r)

    doc = {"name": manifest["name"], "sources": sources, "reviews": all_reviews, "synthesis": None}
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tagged = sum(1 for r in all_reviews if r.get("tags"))
    print(f"{len(all_reviews)} отзывов, {tagged} с тегами -> {args.out}")


if __name__ == "__main__":
    main()
