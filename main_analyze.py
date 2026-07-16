"""Проходит по несанализированным отзывам, вызывает Sentiment Analyst, пишет теги и тональность."""
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
)
from agents.sentiment_analyst import analyze_review, compute_reply_deadline

ZONE_CATEGORIES = {"зона клуба", "подразделение"}


def _normalize_zone(zone: str | None, known_zones: set[str]) -> str | None:
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
        except RateLimitError as e:
            remaining = len(reviews) - i
            print(f"\nЛимит провайдера LLM исчерпан. Разобрано {i} из {len(reviews)}, осталось {remaining}.")
            print(f"Детали: {e}")
            print("Останавливаюсь — перезапусти main_analyze.py позже, необработанные отзывы подхватятся сами.")
            break
        except Exception as e:
            print(f"[review {review['id']}] ОШИБКА анализа: {e}")
            continue

        deadline = compute_reply_deadline(review["review_date"], result["sentiment"], reply_sla_hours)
        update_review_sentiment(
            conn,
            review["id"],
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
                insert_tag_if_new(conn, tag, category)
            zone = aspect.get("zone")
            zone = zone.strip().lower() if zone else None
            zone = _normalize_zone(zone, known_zones)
            insert_review_tag(conn, review["id"], tag, aspect["tag_sentiment"], aspect.get("tag_evidence", ""), zone)

        tags_str = ", ".join(f"{a['tag']}:{a['tag_sentiment']}" for a in result.get("aspects", []))
        print(f"[review {review['id']}] {result['sentiment']} ({result['sentiment_score']}/10) — {tags_str or 'без тем'}")

    conn.close()


if __name__ == "__main__":
    main()
