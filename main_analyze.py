"""Проходит по несанализированным отзывам, вызывает Sentiment Analyst, пишет теги и тональность."""
from core.llm_provider import RateLimitError

from core.config import load_config
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


def main():
    cfg = load_config()
    conn = get_connection()
    init_db(conn)
    seed_category_dictionary(conn, cfg["categories"])
    seed_tag_dictionary(conn, cfg["tags"])

    tag_dictionary = get_tag_dictionary(conn, active_only=True)
    category_dictionary = get_category_dictionary(conn)
    active_tags = {t["tag"] for t in tag_dictionary}
    known_categories = {c["category"] for c in category_dictionary}
    reply_sla_hours = cfg["reply_sla_hours"]

    reviews = fetch_unanalyzed_reviews(conn)
    print(f"К разбору: {len(reviews)} отзывов")

    for i, review in enumerate(reviews):
        try:
            result = analyze_review(review["text"] or "", review["rating"], tag_dictionary, category_dictionary)
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
            insert_review_tag(conn, review["id"], tag, aspect["tag_sentiment"], aspect.get("tag_evidence", ""))

        tags_str = ", ".join(f"{a['tag']}:{a['tag_sentiment']}" for a in result.get("aspects", []))
        print(f"[review {review['id']}] {result['sentiment']} ({result['sentiment_score']}/10) — {tags_str or 'без тем'}")

    conn.close()


if __name__ == "__main__":
    main()
