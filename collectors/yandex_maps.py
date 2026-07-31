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
# Ответ организации ЛЕНИВО рендерится по клику — не просто CSS-скрыт, элемента
# нет в DOM вообще, пока не нажать (найдено 2026-07-31: реальный клуб с 49
# видимыми кнопками "показать ответ" на первых скроллах отдавал 0 обнаруженных
# ответов, потому что .business-review-comment — несуществующий класс, реальный
# контейнер business-review-comment__comment появляется только после клика).
COMMENT_EXPAND_SELECTOR = ".business-review-view__comment-expand"


def _reviews_url(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/reviews") else url + "/reviews/"


def _parse_block(block) -> dict | None:
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
        return None

    comment_el = block.select_one(".business-review-comment__comment")
    reply_text = None
    if comment_el is not None:
        # Внутри контейнера есть заголовок "Official response <дата>" отдельным
        # блоком — берём именно __bubble (чистый текст ответа), не весь
        # контейнер, иначе заголовок попадёт в reply_text.
        try:
            reply_text_el = comment_el.select_one(".business-review-comment-content__bubble") or comment_el
            reply_text = reply_text_el.get_text(" ", strip=True) or None
        except Exception:
            reply_text = None

    return {
        "external_id": synthetic_id(author, date, text[:80]),
        "author": author,
        "rating": rating,
        "text": text,
        "date": date,
        "reply_status": "replied" if comment_el is not None else "pending",
        "reply_text": reply_text,
    }


def fetch_reviews(url: str, max_scrolls: int = 8) -> list[dict]:
    reviews_url = _reviews_url(url)
    collected: dict[str, dict] = {}

    def _harvest_visible(page) -> None:
        # Раскрыть обрезанный текст ПЕРЕД разбором этого раунда — длинные отзывы
        # догружаются по клику (реальный AJAX-подгруз остатка, не просто CSS
        # line-clamp), без этого текст обрывается на полуслове с многоточием.
        try:
            page.eval_on_selector_all(EXPAND_SELECTOR, "els => els.forEach(e => e.click())")
            page.eval_on_selector_all(COMMENT_EXPAND_SELECTOR, "els => els.forEach(e => e.click())")
            page.wait_for_timeout(500)
        except Exception:
            pass
        soup = BeautifulSoup(page.content(), "html.parser")
        for block in soup.select(REVIEW_SELECTOR):
            parsed = _parse_block(block)
            if parsed:
                collected[parsed["external_id"]] = parsed

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(reviews_url, timeout=30000, wait_until="networkidle")
        try:
            page.wait_for_selector(REVIEW_SELECTOR, timeout=10000)
        except Exception:
            browser.close()
            return []

        # Подгрузка идёт ПАЧКАМИ, не по одному отзыву на скролл — на реальном
        # клубе с 300+ отзывами счётчик стоял на месте ~6 скроллов подряд, потом
        # разом прыгал на 50 (найдено 2026-07-30). Порог "остановиться после 2
        # пустых скроллов" обрывал сбор прямо посреди такой паузы. Терпим 10
        # подряд пустых скроллов, прежде чем считать, что подгружать нечего.
        _harvest_visible(page)
        prev_total = len(collected)
        stale_streak = 0
        for _ in range(max_scrolls):
            try:
                page.locator(REVIEWS_CONTAINER_SELECTOR).hover()
            except Exception:
                pass
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1200)
            _harvest_visible(page)
            if len(collected) == prev_total:
                stale_streak += 1
                if stale_streak >= 10:
                    break
            else:
                stale_streak = 0
            prev_total = len(collected)

        browser.close()

    return list(collected.values())
