"""Проходит по несанализированным отзывам, вызывает Sentiment Analyst, пишет теги и тональность.

persist_analysis()/normalize_zone() ниже — общий путь записи результата в БД,
переиспользуется main_reanalyze.py (импортирует их отсюда). До 2026-07-17 эти два
скрипта дублировали логику с расхождением: main_reanalyze.py не нормализовал zone
и не сохранял evidence/review_id для новых pending-тегов (см. CHANGELOG) — теперь
один код на оба сценария."""
from core.llm_provider import RateLimitError

from core.config import load_config, load_few_shot_examples
from core.db import (
    get_connection,
    init_db,
    seed_tag_dictionary,
    seed_category_dictionary,
    get_tag_dictionary,
    get_category_dictionary,
    fetch_unanalyzed_reviews,
    update_review_sentiment,
    insert_review_tag,
    insert_tag_if_new,
    get_tags_pending_notification,
    mark_tag_notified,
    get_location_name,
)
from agents.sentiment_analyst import analyze_review, compute_reply_deadline
from agents.notifier import send_message, format_new_tag_approval_message

ZONE_CATEGORIES = {"зона клуба", "подразделение"}


def normalize_zone(zone: str | None, known_zones: set[str]) -> str | None:
    """Сопоставляет свободный текст от модели с известным местом клуба — без
    этого счётчик зонального алерта дробится на варианты написания одного и
    того же места (найдено 2026-07-16: "женская раздевалка" вместо
    "раздевалка" — 4 негативных упоминания на зону "раздевалка" не сложились
    бы в одно число). Точное совпадение — самый частый случай. Иначе ищем
    известную зону КАК ПОДСТРОКУ (модель обычно уточняет, а не сокращает —
    "женская раздевалка" содержит "раздевалка", не наоборот). Без совпадения —
    оставляем как есть, не выдумываем: это либо новое место, которого нет в
    словаре тегов, либо формулировка, которую стоит разобрать вручную."""
    if not zone:
        return None
    if zone in known_zones:
        return zone
    for known in known_zones:
        if known in zone:
            return known
    return zone


def persist_analysis(
    conn,
    review_id: int,
    review_date: str | None,
    result: dict,
    reply_sla_hours: dict,
    active_tags: set[str],
    known_categories: set[str],
    known_zones: set[str],
) -> str:
    """Записывает результат Sentiment Analyst (тональность + пер-аспектные теги
    с zone-нормализацией и evidence для pending-тегов) для одного review_id.
    Общий путь для main_analyze.py (новые отзывы) и main_reanalyze.py
    (переанализ существующих) — до 2026-07-17 они расходились: reanalyze не
    нормализовал zone и не сохранял evidence/review_id для новых тегов, что
    оставляло approval-карточку без контекста ("отзыв не найден") и позволяло
    вариантам написания зоны (найдено на проде daudelsport: "женская
    раздевалка", "бар") дробить счётчик зонального алерта. Возвращает строку
    "tag:sentiment, ..." для лога вызывающей стороны."""
    deadline = compute_reply_deadline(review_date, result["sentiment"], reply_sla_hours)
    update_review_sentiment(
        conn,
        review_id,
        result["sentiment"],
        result["sentiment_score"],
        result["sentiment_reasoning"],
        result["urgency"],
        deadline,
    )

    for aspect in result.get("aspects", []):
        tag = aspect["tag"].strip().lower()
        # Не доверяем флагу is_new от модели вслепую — сверяем с реальным активным
        # словарём в коде. Модель может ошибочно решить, что тег уже существует
        # (например, перепутать написание "atmosfera" с "атмосфера") и не пометить
        # его как новый — тогда он проскочит мимо утверждения. Здесь этого не будет.
        if tag not in active_tags:
            category = aspect.get("category")
            if category not in known_categories:
                category = "не определено"
            insert_tag_if_new(conn, tag, category, aspect.get("tag_evidence", ""), review_id)
        zone = aspect.get("zone")
        zone = zone.strip().lower() if zone else None
        zone = normalize_zone(zone, known_zones)
        insert_review_tag(conn, review_id, tag, aspect["tag_sentiment"], aspect.get("tag_evidence", ""), zone)

    return ", ".join(f"{a['tag']}:{a['tag_sentiment']}" for a in result.get("aspects", [])) or "без тем"


def main():
    cfg = load_config()
    few_shot_examples = load_few_shot_examples()
    conn = get_connection()
    init_db(conn)
    seed_category_dictionary(conn, cfg["categories"])
    seed_tag_dictionary(conn, cfg["tags"])

    tag_dictionary = get_tag_dictionary(conn, active_only=True)
    category_dictionary = get_category_dictionary(conn)
    active_tags = {t["tag"] for t in tag_dictionary}
    known_categories = {c["category"] for c in category_dictionary}
    known_zones = {t["tag"] for t in tag_dictionary if t["category"] in ZONE_CATEGORIES}
    reply_sla_hours = cfg["reply_sla_hours"]

    reviews = fetch_unanalyzed_reviews(conn)
    print(f"К разбору: {len(reviews)} отзывов")

    for i, review in enumerate(reviews):
        try:
            result = analyze_review(review["text"] or "", review["rating"], tag_dictionary, category_dictionary, few_shot_examples)
            tags_str = persist_analysis(
                conn, review["id"], review["review_date"], result, reply_sla_hours,
                active_tags, known_categories, known_zones,
            )
        except RateLimitError as e:
            remaining = len(reviews) - i
            print(f"\nЛимит провайдера LLM исчерпан. Разобрано {i} из {len(reviews)}, осталось {remaining}.")
            print(f"Детали: {e}")
            print("Останавливаюсь — перезапусти main_analyze.py позже, необработанные отзывы подхватятся сами.")
            break
        except Exception as e:
            # Ловит и сбой самого вызова LLM, и битый/неполный JSON от модели
            # (например отсутствующий ключ "sentiment") внутри persist_analysis —
            # оба случая должны просто пропустить этот отзыв, не ронять весь прогон.
            print(f"[review {review['id']}] ОШИБКА анализа: {e}")
            continue

        print(f"[review {review['id']}] {result['sentiment']} ({result['sentiment_score']}/10) — {tags_str}")

    # Approval новых тегов (см. PLAN.md) — текстовое уведомление БЕЗ кнопок,
    # само решение принимается в дашборде (webapp), не в Telegram — выбор
    # категории кнопками упирается в лимит callback_data (64 байта), см.
    # agents/notifier.py: format_new_tag_approval_message. Одно сообщение на
    # тег. location_name для читаемости — вся конфигурация одноклиентная
    # (одна точка на конфиг), берём первую попавшуюся.
    pending = get_tags_pending_notification(conn)
    if pending:
        first_location = cfg["client"]["locations"][0]["id"]
        location_name = get_location_name(conn, first_location)
        for p in pending:
            text = format_new_tag_approval_message(p, location_name)
            try:
                send_message(text)
                mark_tag_notified(conn, p["tag"])
            except Exception as e:
                print(f"Не удалось отправить approval-уведомление на тег '{p['tag']}': {e}")
        print(f"Отправлено approval-уведомлений: {len(pending)}")

    conn.close()


if __name__ == "__main__":
    main()
