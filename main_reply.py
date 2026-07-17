"""Готовит черновики ответов для отзывов, ожидающих ответа. Ничего не публикует —
только пишет reply_draft/internal_note в БД, дальше их видит менеджер через Notifier."""
from core import db
from core.config import load_config
from agents.reply_strategist import load_tone_guide, draft_reply


def main():
    cfg = load_config()
    conn = db.get_connection()
    db.init_db(conn)

    tone_cfg = cfg["tone_of_voice"]
    if tone_cfg["source"] != "document":
        print(f"tone_of_voice.source={tone_cfg['source']} не поддержан пока, нужен 'document'")
        return
    tone_guide = load_tone_guide(tone_cfg["path"])

    reviews = db.get_reviews_needing_reply(conn)
    print(f"Черновиков к подготовке: {len(reviews)}")

    for review in reviews:
        tags = db.get_review_tags(conn, review["id"])
        # Только НЕГАТИВНЫЕ теги этого отзыва — иначе позитивный отзыв с тегом
        # "персонал":positive подмешивал бы контекст открытого алерта по теме
        # "персонал" (набранного ДРУГИМИ отзывами), и internal_note получалась
        # противоречивой: "3-я жалоба на персонал" на отзыве, где персонал хвалят
        # (см. CHANGELOG 2026-07-15).
        negative_tag_names = [t["tag"] for t in tags if t["tag_sentiment"] == "negative"]
        alert_context = db.get_active_alerts_for_tags(conn, review["location_id"], negative_tag_names)

        try:
            result = draft_reply(dict(review), tags, tone_guide, alert_context)
            db.update_reply_draft(
                conn, review["id"], result["review_type"], result["reply_draft"], result.get("internal_note")
            )
        except Exception as e:
            # Ловит и сбой вызова LLM, и битый/неполный JSON от модели (например
            # отсутствующий ключ "review_type") — оба случая пропускают отзыв,
            # не роняя весь прогон (тот же принцип, что в main_analyze.py).
            print(f"[review {review['id']}] ОШИБКА: {e}")
            continue

        flag = " ⚠️ ТРЕБУЕТ ПРОВЕРКИ" if result["review_type"] == "требует_проверки" else ""
        print(f"[review {review['id']}] {result['review_type']}{flag}")

    conn.close()


if __name__ == "__main__":
    main()
