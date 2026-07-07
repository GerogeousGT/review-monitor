"""Сборщик отзывов с 2ГИС — через Apify (zen-studio/2gis-reviews-scraper), не напрямую.

Собственный Playwright-скрейпер стабильно ловит 403 antibot — проверено не только с
датацентровых IP, но и с обычного жилого провайдера (VNPT, Вьетнам). Похоже, дело не
в "облако vs дом", а в гео (не-российский IP) и/или в фингерпринте автоматизированного
браузера — разбираться дальше себе дороже, когда есть готовый сервис за $1/1000 отзывов.
См. PLAN.md.

Тарификация Apify — за отзыв В ВЫДАЧЕ, не за сам запрос. Поэтому max_reviews в
client_config.yaml должен быть маленьким (10-30), а не "выгрузить всё" на каждый прогон.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

from core.env import load_env

from .base import synthetic_id

load_env()

ACTOR = "zen-studio~2gis-reviews-scraper"
API_URL = f"https://api.apify.com/v2/actors/{ACTOR}/run-sync-get-dataset-items"


def fetch_reviews(url: str, max_reviews: int = 10, lookback_days: int = 60) -> list[dict]:
    token = os.environ["APIFY_API_TOKEN"]
    clean_url = url.split("/tab/")[0]  # actor ожидает ссылку на карточку, не на конкретную вкладку

    # Без этого фильтра выдача не строго по дате — среди свежих отзывов может затесаться
    # древний (проверено: между майскими 2026 попался отзыв за 2023). Фильтр по дате и
    # экономит квоту (тариф — за отзыв в выдаче), и убирает нерелевантный шум.
    start_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    resp = requests.post(
        API_URL,
        params={"token": token},
        json={
            "startUrls": [clean_url],
            "maxReviews": max_reviews,
            "reviewsStartDate": start_date,
        },
        timeout=180,
    )
    resp.raise_for_status()
    items = resp.json()

    results = []
    for item in items:
        text = item.get("text") or ""
        author = item.get("authorName")
        rating = item.get("rating")
        date = item.get("dateCreated")
        external_id = str(item.get("reviewId") or synthetic_id(author, date, text[:80]))

        has_reply = bool(item.get("officialAnswer"))

        results.append(
            {
                "external_id": external_id,
                "author": author,
                "rating": int(rating) if rating is not None else None,
                "text": text,
                "date": date,
                "reply_status": "replied" if has_reply else "pending",
            }
        )
    return results
