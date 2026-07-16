"""Переанализ КОНКРЕТНЫХ уже разобранных отзывов через текущий словарь тегов —
для случаев, когда словарь калибровался ПОСЛЕ того, как отзыв был проанализирован
(найдено 2026-07-16: отзывы про возврат денег размечены до появления тега "возврат
средств", из-за чего одна тема размазана по трём ложным алертам — цена/персонал/
возврат средств, см. CHANGELOG/PLAN.md).

НЕ трогает reply_draft/reply_status/notified_at — черновики ответов и факт
уведомления в Telegram не переделываются. НЕ гонять main_notify.py после этого —
отзывы не новые, повторная отправка карточек в Telegram была бы спамом (см. чек-лист
--backfill в client_config.yaml, тот же принцип).

После переанализа — пересчитать алерты вручную: main_alerts.py.

Использование:
  CLIENT_SLUG=<slug> .venv/Scripts/python main_reanalyze.py <review_id> [<review_id> ...]
"""
import sys

from core.llm_provider import RateLimitError

from core.config import load_config, load_few_shot_examples
from core.db import (
    get_connection,
    init_db,
    seed_tag_dictionary,
    seed_category_dictionary,
    get_tag_dictionary,
    get_category_dictionary,
    update_review_sentiment,
    insert_review_tag,
    insert_tag_if_new,
)
from agents.sentiment_analyst import analyze_review, compute_reply_deadline


def main():
    if len(sys.argv) < 2:
        print("Использование: main_reanalyze.py <review_id> [<review_id> ...]")
        sys.exit(1)
    review_ids = [int(x) for x in sys.argv[1:]]

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
    reply_sla_hours = cfg["reply_sla_hours"]

    for review_id in review_ids:
        review = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            print(f"[review {review_id}] не найден, пропуск")
            continue

        old_tags = conn.execute(
            "SELECT tag, tag_sentiment FROM review_tags WHERE review_id=?", (review_id,)
        ).fetchall()
        old_str = ", ".join(f"{t['tag']}:{t['tag_sentiment']}" for t in old_tags) or "без тем"

        try:
            result = analyze_review(review["text"] or "", review["rating"], tag_dictionary, category_dictionary, few_shot_examples)
        except RateLimitError as e:
            print(f"Лимит провайдера LLM исчерпан на review {review_id}: {e}")
            break
        except Exception as e:
            print(f"[review {review_id}] ОШИБКА анализа: {e}")
            continue

        deadline = compute_reply_deadline(review["review_date"], result["sentiment"], reply_sla_hours)
        update_review_sentiment(
            conn,
            review_id,
            result["sentiment"],
            result["sentiment_score"],
            result["sentiment_reasoning"],
            result["urgency"],
            deadline,
        )

        conn.execute("DELETE FROM review_tags WHERE review_id=?", (review_id,))
        conn.commit()

        for aspect in result.get("aspects", []):
            tag = aspect["tag"].strip().lower()
            if tag not in active_tags:
                category = aspect.get("category")
                if category not in known_categories:
                    category = "не определено"
                insert_tag_if_new(conn, tag, category)
            zone = aspect.get("zone")
            zone = zone.strip().lower() if zone else None
            insert_review_tag(conn, review_id, tag, aspect["tag_sentiment"], aspect.get("tag_evidence", ""), zone)

        new_str = ", ".join(f"{a['tag']}:{a['tag_sentiment']}" for a in result.get("aspects", [])) or "без тем"
        print(f"[review {review_id}] БЫЛО: {old_str}")
        print(f"[review {review_id}] СТАЛО: {new_str}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
