"""Сборщик отзывов с Яндекс Карт.

Тональность/рейтинг/дата берутся из микроразметки schema.org (itemprop),
поэтому не зависят от локали интерфейса. Явного ID отзыва Яндекс не отдаёт —
используется синтетический хэш от автора+даты+начала текста.
"""
import re
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .base import USER_AGENT, synthetic_id

REVIEWS_CONTAINER_SELECTOR = ".business-reviews-card-view__reviews-container"
REVIEW_SELECTOR = ".business-review-view"
EXPAND_SELECTOR = ".business-review-view__expand"


def _reviews_url(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/reviews") else url + "/reviews/"


def fetch_reviews(url: str, max_scrolls: int = 8) -> list[dict]:
    reviews_url = _reviews_url(url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(reviews_url, timeout=30000, wait_until="networkidle")
        try:
            page.wait_for_selector(REVIEW_SELECTOR, timeout=10000)
        except Exception:
            browser.close()
            return []

        prev_count = 0
        for _ in range(max_scrolls):
            count = page.locator(REVIEW_SELECTOR).count()
            if count == prev_count:
                break
            prev_count = count
            try:
                page.locator(REVIEWS_CONTAINER_SELECTOR).hover()
            except Exception:
                pass
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1200)

        # Длинные отзывы визуально обрезаны (CSS line-clamp) и ДОГРУЖАЮТСЯ по клику —
        # это не просто "показать скрытое", а реальный AJAX-подгруз остатка текста.
        # Без этого шага текст обрывается на полуслове с многоточием.
        try:
            page.eval_on_selector_all(EXPAND_SELECTOR, "els => els.forEach(e => e.click())")
            page.wait_for_timeout(1500)
        except Exception:
            pass

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    results = []
    for block in soup.select(REVIEW_SELECTOR):
        name_el = block.select_one('[itemprop="author"] [itemprop="name"]')
        author = name_el.get_text(strip=True) if name_el else None

        rating_el = block.select_one(".business-rating-badge-view__stars")
        rating = None
        if rating_el and rating_el.get("aria-label"):
            m = re.search(r"Rating (\d+)", rating_el["aria-label"])
            if m:
                rating = int(m.group(1))

        date_el = block.select_one('meta[itemprop="datePublished"]')
        date = date_el["content"] if date_el else None

        text_el = block.select_one('[itemprop="reviewBody"]')
        text = ""
        if text_el:
            # На случай, если тумблер "ещё/свернуть" не исчез из DOM после клика —
            # вырезаем его, чтобы его подпись не попала в текст отзыва.
            for toggle in text_el.select(".business-review-view__expand, .spoiler-view__button"):
                toggle.decompose()
            text = text_el.get_text(" ", strip=True).rstrip("… ").strip()

        if not text and rating is None:
            continue

        has_reply = block.select_one(".business-review-comment") is not None

        results.append(
            {
                "external_id": synthetic_id(author, date, text[:80]),
                "author": author,
                "rating": rating,
                "text": text,
                "date": date,
                "reply_status": "replied" if has_reply else "pending",
            }
        )
    return results
