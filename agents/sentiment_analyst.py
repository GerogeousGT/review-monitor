"""LLM-агент: аспектная тональность отзыва (не отзыв целиком, а тема внутри него).

Модель обязана выбирать теги из переданного словаря; если ни один не подходит,
может предложить новый — он не попадает в active-словарь автоматически
(см. db.insert_tag_if_new), а требует ручного утверждения.
"""
import json
from datetime import datetime, timedelta, timezone

from core.dates import parse_review_date
from core.llm_provider import get_client, MODEL

SYSTEM_PROMPT = """Ты анализируешь отзыв клиента фитнес-клуба. Твоя задача — определить:
1. Общую тональность отзыва целиком (positive/neutral/negative) и её интенсивность (score 1-10,
   где для negative — 10 это максимально резкий негатив, для positive — 10 максимально горячая похвала).
2. Отдельно — тональность КАЖДОЙ конкретной темы, которая явно упомянута в тексте, даже если
   общая тональность отзыва другая (например, отзыв в целом хвалебный, но одна фраза жалуется
   на конкретную вещь — эта тема должна получить negative, несмотря на общий позитив).

## Категории (используются только для новых тегов — см. ниже)
{category_list}

## Словарь существующих тегов
{tag_list}

## Как выбрать тег
Сначала пытайся найти тег в словаре выше, чьё описание покрывает упомянутое в отзыве явление —
даже если формулировка в отзыве другая (например, жалоба на "духоту в зале" — это тег
"тренажёрный зал", а не повод придумывать новый тег "духота" или "вентиляция").

Предлагай новый тег ТОЛЬКО если ни один существующий тег и его описание не подходят по смыслу.
Правила для нового тега:
- короткое название на русском (1-2 слова, нижний регистр)
- обязательно укажи "is_new": true
- обязательно укажи "category" ИЗ СПИСКА КАТЕГОРИЙ ВЫШЕ — не изобретай новую категорию
- не предлагай тег, название которого совпадает или почти совпадает с названием категории
  (например, не предлагай тег "оснащение" — это категория, а не тема)

Если отзыв не содержит явной жалобы или похвалы по конкретной теме (например, короткое "супер!"
без деталей) — верни пустой список aspects, это нормально.

Ответь СТРОГО в формате JSON, без пояснений вне JSON:
{{
  "sentiment": "positive|neutral|negative",
  "sentiment_score": 1-10,
  "sentiment_reasoning": "краткое объяснение на русском, почему такая оценка",
  "urgency": true|false,
  "aspects": [
    {{"tag": "...", "tag_sentiment": "positive|neutral|negative", "tag_evidence": "точная цитата из отзыва", "is_new": false, "category": null}}
  ]
}}
"""


def analyze_review(text: str, rating: int | None, tag_dictionary: list[dict], category_dictionary: list[dict]) -> dict:
    category_list = "\n".join(f"- {c['category']}: {c['description']}" for c in category_dictionary)
    tag_list = "\n".join(f"- {t['tag']} ({t['category']}): {t.get('description') or 'без описания'}" for t in tag_dictionary)
    system = SYSTEM_PROMPT.format(category_list=category_list, tag_list=tag_list)
    user = f"Рейтинг отзыва: {rating if rating is not None else 'не указан'}/5\nТекст отзыва: {text}"

    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    return data


def compute_reply_deadline(review_date_iso: str, sentiment: str, reply_sla_hours: dict) -> str | None:
    hours = reply_sla_hours.get(sentiment)
    if hours is None:
        return None
    try:
        review_dt = parse_review_date(review_date_iso)
    except (ValueError, AttributeError):
        review_dt = datetime.now(timezone.utc)
    deadline = review_dt + timedelta(hours=hours)
    return deadline.isoformat()
