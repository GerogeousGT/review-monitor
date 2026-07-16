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
Тег — это ТЕМА (что произошло: оборудование, чистота, персонал), а не место. Сначала пытайся
найти тег в словаре выше, чьё описание покрывает упомянутое в отзыве явление — даже если
формулировка в отзыве другая (например, жалоба на "духоту в зале" — это тег "вентиляция",
а не повод придумывать новый тег "духота").

Не путай тег с зоной: если в словаре есть отдельный тег под конкретную тему (например
"оборудование"), используй ЕГО, а не тег зоны/помещения, даже если зона тоже упомянута рядом.
Пример: "Тренажерка супер, оборудование все качественное" — тег "оборудование" (это тема, на
которую хвалят), зона "тренажёрный зал" указывается ОТДЕЛЬНО в поле zone (см. ниже) — тег
"тренажёрный зал" здесь НЕПРАВИЛЬНЫЙ, это была бы подмена конкретной темы общим местом.
Тег зоны/помещения (например "тренажёрный зал", "бассейн") выбирай тегом, только если сам
отзыв про помещение как таковое без конкретной темы внутри (теснота, планировка, интерьер) —
а не всегда, когда тема просто произошла в этом помещении.

Некоторые теги в словаре описывают не тему-предмет, а САМ ФАКТ КОММУНИКАЦИИ (например
"информирование" — клиента не предупредили заранее о чём-то). Если в тексте есть явный маркер
такого факта ("не сообщат", "не предупредили", "первый раз слышу", "оказывается") — ставь тег
коммуникации ДОПОЛНИТЕЛЬНО к тегу темы, о которой не предупредили, а не вместо него (два
отдельных аспекта на одну фразу — это нормально, а не дублирование).

Предлагай новый тег ТОЛЬКО если ни один существующий тег и его описание не подходят по смыслу.
Правила для нового тега:
- короткое название на русском (1-2 слова, нижний регистр)
- обязательно укажи "is_new": true
- обязательно укажи "category" ИЗ СПИСКА КАТЕГОРИЙ ВЫШЕ — не изобретай новую категорию
- не предлагай тег, название которого совпадает или почти совпадает с названием категории
  (например, не предлагай тег "оснащение" — это категория, а не тема)

## Как заполнить zone
Отдельно от тега — если из текста ясно, к какому месту/зоне клуба относится тема (тренажёрный
зал, бассейн, групповые программы, раздевалка и т.п.), укажи это в "zone" (короткое название
зоны на русском). Если место не упомянуто или тема не привязана к конкретной зоне (например,
общая жалоба на цену или на организацию в целом) — "zone": null. Zone не обязана совпадать
с названием тега из словаря — это свободное поле для места, а не второй тег.

Если отзыв не содержит явной жалобы или похвалы по конкретной теме (например, короткое "супер!"
без деталей) — верни пустой список aspects, это нормально.

{examples_block}## Формат ответа

Ответь СТРОГО в формате JSON, без пояснений вне JSON:
{{
  "sentiment": "positive|neutral|negative",
  "sentiment_score": 1-10,
  "sentiment_reasoning": "краткое объяснение на русском, почему такая оценка",
  "urgency": true|false,
  "aspects": [
    {{"tag": "...", "tag_sentiment": "positive|neutral|negative", "tag_evidence": "точная цитата из отзыва", "is_new": false, "category": null, "zone": null}}
  ]
}}
"""


def _format_examples_block(few_shot_examples: list[dict] | None) -> str:
    """Примеры — per-client (см. clients/<slug>/few_shot_examples.yaml), не общий
    шаблон: generic-примеры на чужих тегах путают модель больше, чем помогают
    (найдено 2026-07-15 — см. PLAN.md). Пустой список — нормально для клиента без
    калибровки, промпт работает и без примеров."""
    if not few_shot_examples:
        return ""
    parts = ["## Примеры разбора (подобраны под реальные путаницы этого клиента)\n"]
    for ex in few_shot_examples:
        output_json = json.dumps(ex["output"], ensure_ascii=False, indent=2)
        parts.append(f'Отзыв: "{ex["text"]}" (рейтинг {ex["rating"]}/5)\n{output_json}\n')
    return "\n".join(parts) + "\n"


def analyze_review(
    text: str,
    rating: int | None,
    tag_dictionary: list[dict],
    category_dictionary: list[dict],
    few_shot_examples: list[dict] | None = None,
) -> dict:
    category_list = "\n".join(f"- {c['category']}: {c['description']}" for c in category_dictionary)
    tag_list = "\n".join(f"- {t['tag']} ({t['category']}): {t.get('description') or 'без описания'}" for t in tag_dictionary)
    examples_block = _format_examples_block(few_shot_examples)
    system = SYSTEM_PROMPT.format(category_list=category_list, tag_list=tag_list, examples_block=examples_block)
    user = f"Рейтинг отзыва: {rating if rating is not None else 'не указан'}/5\nТекст отзыва: {text}"

    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
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
