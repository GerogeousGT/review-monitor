"""Сборщик отзывов с Zoon.

ID отзыва — нативный, из id="comment<hex>" на карточке отзыва (не синтетический).
Рейтинг и дата — из микроразметки schema.org.
"""
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .base import USER_AGENT

REVIEW_LIST_SELECTOR = "ul.js-comment-list"
SHOW_MORE_SELECTOR = ".show-more-button"


def fetch_reviews(url: str, max_clicks: int = 5) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(1500)
        try:
            page.click("text=Отзывы", timeout=5000)
            page.wait_for_timeout(2000)
        except Exception:
            pass

        for _ in range(max_clicks):
            btn = page.locator(SHOW_MORE_SELECTOR).first
            if btn.count() == 0 or not btn.is_visible():
                break
            try:
                btn.click(timeout=3000)
                page.wait_for_timeout(1500)
            except Exception:
                break

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    review_list = soup.select_one(REVIEW_LIST_SELECTOR)
    if review_list is None:
        return []

    results = []
    # Только отзывы верхнего уровня: официальный ответ клуба на Zoon рендерится
    # как вложенный <li class="comment-item"> ВНУТРИ отзыва, на который отвечают —
    # если не ограничиться top-level, ответ посчитается отдельным "отзывом".
    for block in review_list.find_all("li", recursive=False):
        external_id = block.get("id", "").replace("comment", "") or None

        author_el = block.select_one('[itemprop="author"] [itemprop="name"]')
        author = author_el.get_text(strip=True) if author_el else None

        rating_el = block.select_one('[itemprop="ratingValue"]')
        rating = None
        if rating_el and rating_el.get("content"):
            try:
                rating = round(float(rating_el["content"]))
            except ValueError:
                pass

        date_el = block.select_one('[itemprop="datePublished"]')
        date = date_el["content"] if date_el else None

        text_el = block.select_one('[itemprop="reviewBody"]')
        text = text_el.get_text(" ", strip=True) if text_el else ""

        has_reply = block.select_one("li.comment-item") is not None

        if not external_id:
            continue

        results.append(
            {
                "external_id": external_id,
                "author": author,
                "rating": rating,
                "text": text,
                "date": date,
                "reply_status": "replied" if has_reply else "pending",
            }
        )
    return results
