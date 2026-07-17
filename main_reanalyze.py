"""Переанализ КОНКРЕТНЫХ уже разобранных отзывов через текущий словарь тегов —
для случаев, когда словарь калибровался ПОСЛЕ того, как отзыв был проанализирован
(найдено 2026-07-16: отзывы про возврат денег размечены до появления тега "возврат
средств", из-за чего одна тема размазана по трём ложным алертам — цена/персонал/
возврат средств, см. CHANGELOG/PLAN.md).

НЕ трогает reply_draft/reply_status/notified_at — черновики ответов и факт
уведомления в Telegram не переделываются. НЕ гонять main_notify.py после этого —
отзывы не новые, повторная отправка карточек в Telegram была бы спамом (см. чек-лист
--backfill в client_config.yaml, тот же принцип).

Запись результата (тональность + теги + zone-нормализация + evidence для pending-
тегов) идёт через main_analyze.persist_analysis() — общий код с main_analyze.py
(исправлено 2026-07-17: раньше здесь была отдельная частичная копия без
нормализации zone и без evidence/review_id для новых тегов, см. CHANGELOG).

После переанализа — пересчитать алерты вручную: main_alerts.py.

Использование:
  CLIENT_SLUG=<slug> .venv/Scripts/python main_reanalyze.py <review_id> [<review_id> ...]
"""
import sys

from core.llm_provider import RateLimitError

from core.config import load_config, load_few_shot_examples
from core.db import get_connection, init_db, seed_tag_dictionary, seed_category_dictionary, get_tag_dictionary, get_category_dictionary
from agents.sentiment_analyst import analyze_review
from main_analyze import persist_analysis, ZONE_CATEGORIES


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
    known_zones = {t["tag"] for t in tag_dictionary if t["category"] in ZONE_CATEGORIES}
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
            # Старые теги удаляются ПОСЛЕ успешного вызова LLM — сбой анализа не
            # должен стирать существующую разметку отзыва без замены.
            conn.execute("DELETE FROM review_tags WHERE review_id=?", (review_id,))
            conn.commit()
            new_str = persist_analysis(
                conn, review_id, review["review_date"], result, reply_sla_hours,
                active_tags, known_categories, known_zones,
            )
        except RateLimitError as e:
            print(f"Лимит провайдера LLM исчерпан на review {review_id}: {e}")
            break
        except Exception as e:
            # Ловит и сбой вызова LLM, и битый/неполный JSON от модели внутри
            # persist_analysis — пропускаем отзыв, не роняем весь прогон.
            print(f"[review {review_id}] ОШИБКА анализа: {e}")
            continue

        print(f"[review {review_id}] БЫЛО: {old_str}")
        print(f"[review {review_id}] СТАЛО: {new_str}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
