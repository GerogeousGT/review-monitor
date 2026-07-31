"""Сборщик отзывов с 2ГИС — через Apify (zen-studio/2gis-reviews-scraper), не напрямую.

Собственный Playwright-скрейпер стабильно ловит 403 antibot — проверено не только с
датацентровых IP, но и с обычного жилого провайдера (VNPT, Вьетнам). Похоже, дело не
в "облако vs дом", а в гео (не-российский IP) и/или в фингерпринте автоматизированного
браузера — разбираться дальше себе дороже, когда есть готовый сервис за $1/1000 отзывов.
См. PLAN.md.

Тарификация Apify — за отзыв В ВЫДАЧЕ, не за сам запрос. Поэтому max_reviews в
client_config.yaml должен быть маленьким (10-30), а не "выгрузить всё" на каждый прогон.

Асинхронный запуск (не run-sync-get-dataset-items) — синхронный удобный эндпоинт
физически не укладывается в разумный клиентский таймаут на больших выдачах (найдено
2026-07-30 на разовом сборе конкурента: на maxReviews=350/0 стабильно ловили
ReadTimeout на 180с, хотя актор продолжал работать). /runs + поллинг статуса +
отдельный запрос отзывов из датасета — тот же результат, без ограничения по времени
одного HTTP-запроса. Для типичного продуктового прогона (max_reviews=5-30) разница
не заметна — пара лишних запросов, доли секунды.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from core.env import load_env

from .base import synthetic_id

load_env()

ACTOR = "zen-studio~2gis-reviews-scraper"
RUNS_URL = f"https://api.apify.com/v2/acts/{ACTOR}/runs"
POLL_INTERVAL_SECONDS = 5
MAX_WAIT_SECONDS = 540  # с запасом больше уже виденных на практике ~215с на полный сбор


def _parse_item(item: dict) -> dict | None:
    text = item.get("text") or ""
    author = item.get("authorName")
    rating = item.get("rating")
    date = item.get("dateCreated")

    # Пустая запись без текста и рейтинга — не отзыв (видели такое на проблемных
    # прогонах актора, 2026-07-30), тот же фильтр, что в collectors/yandex_maps.py.
    if not text and rating is None:
        return None

    external_id = str(item.get("reviewId") or synthetic_id(author, date, text[:80]))

    official_answer = item.get("officialAnswer")
    reply_text = None
    # Форма officialAnswer в ответе актора не задокументирована — на практике
    # видели то строку, то объект с текстовым полем; берём что есть, не падаем
    # на неожиданной форме (сам факт наличия уже достаточен для reply_status).
    if isinstance(official_answer, str):
        reply_text = official_answer.strip() or None
    elif isinstance(official_answer, dict):
        reply_text = (official_answer.get("text") or official_answer.get("comment") or "").strip() or None

    return {
        "external_id": external_id,
        "author": author,
        "rating": int(rating) if rating is not None else None,
        "text": text,
        "date": date,
        "reply_status": "replied" if official_answer else "pending",
        "reply_text": reply_text,
    }


def fetch_reviews(url: str, max_reviews: int = 10, lookback_days: int = 60, token: str | None = None) -> list[dict]:
    """token — необязательный override (см. scripts/competitors/collect.py:
    разовый полный сбор конкурентов использует ОТДЕЛЬНЫЙ Apify-аккаунт
    (COMPETITORS_APIFY_API_TOKEN), не APIFY_API_TOKEN клиента — чтобы не
    путать биллинг разведки с продуктовым сбором."""
    token = token or os.environ["APIFY_API_TOKEN"]
    clean_url = url.split("/tab/")[0]  # actor ожидает ссылку на карточку, не на конкретную вкладку

    # Без этого фильтра выдача не строго по дате — среди свежих отзывов может затесаться
    # древний (проверено: между майскими 2026 попался отзыв за 2023). Фильтр по дате и
    # экономит квоту (тариф — за отзыв в выдаче), и убирает нерелевантный шум.
    start_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    run_resp = requests.post(
        RUNS_URL,
        params={"token": token},
        json={
            "startUrls": [clean_url],
            "maxReviews": max_reviews,
            "reviewsStartDate": start_date,
        },
        timeout=30,
    )
    run_resp.raise_for_status()
    run = run_resp.json()["data"]

    status_url = f"https://api.apify.com/v2/actor-runs/{run['id']}"
    deadline = time.time() + MAX_WAIT_SECONDS
    data = run
    while data["status"] not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
        if time.time() > deadline:
            raise TimeoutError(f"2GIS actor run {run['id']} не завершился за {MAX_WAIT_SECONDS}с")
        time.sleep(POLL_INTERVAL_SECONDS)
        status_resp = requests.get(status_url, params={"token": token}, timeout=30)
        status_resp.raise_for_status()
        data = status_resp.json()["data"]

    if data["status"] != "SUCCEEDED":
        raise RuntimeError(f"2GIS actor run {run['id']} завершился со статусом {data['status']}")

    items_resp = requests.get(
        f"https://api.apify.com/v2/datasets/{data['defaultDatasetId']}/items",
        params={"token": token, "format": "json"},
        timeout=60,
    )
    items_resp.raise_for_status()
    items = items_resp.json()

    results = []
    for item in items:
        parsed = _parse_item(item)
        if parsed:
            results.append(parsed)
    return results
