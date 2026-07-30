"""Разовый сбор отзывов конкурента с ОДНОЙ площадки — не часть продукта, не по
расписанию (см. PLAN.md "Конкурентная разведка"). Переиспользует настоящие
collectors/*.py, но не main_collect.py — в БД клиента ничего не пишет, только
сырой JSON-список отзывов на диск. Слияние нескольких площадок в один
clients/<slug>/competitors/<comp>.json — отдельный ручной шаг после того, как
сырые данные проверены (см. merge.py).

Полный (не инкрементный, как в проде) сбор — большие лимиты по умолчанию, их
можно переопределить флагами:
  python scripts/competitors/collect.py --platform yandex_maps --url "https://yandex.ru/maps/org/.../reviews/" --out /tmp/fitberri_yandex.json
  python scripts/competitors/collect.py --platform 2gis --url "https://2gis.ru/..." --out /tmp/fitberri_2gis.json  # нужен CLIENT_SLUG в env — 2ГИС тянет APIFY_API_TOKEN клиента
  python scripts/competitors/collect.py --platform zoon --url "https://zoon.ru/..." --out /tmp/fitberri_zoon.json
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors import twogis, yandex_maps, zoon  # noqa: E402

FETCHERS = {
    "yandex_maps": lambda url, o: yandex_maps.fetch_reviews(url, max_scrolls=o.max_scrolls),
    "2gis": lambda url, o: twogis.fetch_reviews(url, max_reviews=o.max_reviews, lookback_days=o.lookback_days),
    "zoon": lambda url, o: zoon.fetch_reviews(url, max_clicks=o.max_clicks),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=sorted(FETCHERS))
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    # Полный разовый сбор, не инкремент — лимиты заметно выше, чем в client_config.yaml продукта.
    parser.add_argument("--max-scrolls", type=int, default=30)
    parser.add_argument("--max-clicks", type=int, default=15)
    parser.add_argument("--max-reviews", type=int, default=200)
    parser.add_argument("--lookback-days", type=int, default=3650)
    args = parser.parse_args()

    reviews = FETCHERS[args.platform](args.url, args)
    for r in reviews:
        r["platform"] = args.platform

    out_path = Path(args.out)
    out_path.write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    replied = sum(1 for r in reviews if r.get("reply_status") == "replied")
    print(f"{len(reviews)} отзывов ({replied} с ответом клуба) -> {out_path}")


if __name__ == "__main__":
    main()
