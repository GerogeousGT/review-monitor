"""Точка входа: проходит по client_config.yaml, собирает отзывы, пишет новые в БД.

--backfill: помечает все вставленные отзывы как уже уведомлённые (notified_at) сразу
здесь, а не оставляет это main_notify.py. Без флага первый же крупный импорт истории
(первое подключение новой площадки/компании, или ручная доливка старых отзывов в БД)
рассылает карточку по КАЖДОМУ отзыву при следующем прогоне main_notify.py — было
проверено на практике (2026-07-08, Даудель Спорт: 90 отзывов долиты в БД без флага,
ближайший плановый main_notify.py разослал все 90 одним пакетом)."""
import sys

from core.config import load_config
from core.db import (
    get_connection,
    init_db,
    ensure_location,
    ensure_platform,
    insert_review_if_new,
    update_platform_checkpoint,
)
from collectors import yandex_maps, zoon, twogis

COLLECTORS = {
    "yandex_maps": yandex_maps.fetch_reviews,
    "zoon": zoon.fetch_reviews,
    "2gis": twogis.fetch_reviews,
}


def main():
    backfill = "--backfill" in sys.argv
    if backfill:
        print("[backfill] режим: новые отзывы будут сразу помечены как уведомлённые, main_notify.py их не разошлёт")

    cfg = load_config()
    conn = get_connection()
    init_db(conn)

    for location in cfg["client"]["locations"]:
        location_id = location["id"]
        ensure_location(conn, location_id, location["name"], location.get("city", ""))

        for platform_cfg in location["platforms"]:
            platform = platform_cfg["type"]
            url = platform_cfg["url"]
            ensure_platform(conn, location_id, platform, url)

            fetch = COLLECTORS.get(platform)
            if fetch is None:
                print(f"[skip] нет коллектора для платформы {platform}")
                continue

            # Прочие поля площадки (кроме type/url/poll_interval_hours — последнее
            # только для расписания cron, не параметр сбора) пробрасываем в fetch()
            # как есть — например max_reviews у 2ГИС.
            extra_kwargs = {
                k: v for k, v in platform_cfg.items() if k not in ("type", "url", "poll_interval_hours")
            }

            print(f"[{location_id}/{platform}] собираю...")
            try:
                reviews = fetch(url, **extra_kwargs)
            except Exception as e:
                print(f"[{location_id}/{platform}] ОШИБКА: {e}")
                continue

            new_count = 0
            latest_id, latest_date = None, None
            for review in reviews:
                if insert_review_if_new(conn, location_id, platform, review, skip_notify=backfill):
                    new_count += 1
                if latest_date is None or (review.get("date") and review["date"] > latest_date):
                    latest_id, latest_date = review["external_id"], review.get("date")

            if latest_id:
                update_platform_checkpoint(conn, location_id, platform, latest_id, latest_date)

            print(f"[{location_id}/{platform}] всего собрано: {len(reviews)}, новых: {new_count}")

    conn.close()


if __name__ == "__main__":
    main()
