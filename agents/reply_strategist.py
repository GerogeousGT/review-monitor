"""LLM-агент: черновик ответа на отзыв с учётом тона компании и контекста алертов.

Классификация негатива ограничена намеренно — см. tone_of_voice.md: "чёрная реклама"
и "троллинг" не отдаются модели на автораспознавание, ошибка здесь (принять реального
клиента за тролля) вредит репутации сильнее, чем лишняя вежливость к настоящему троллю.
"""
import json

from core.env import PROJECT_ROOT
from core.llm_provider import get_client, MODEL


SYSTEM_PROMPT = """Ты пишешь черновик публичного ответа клуба на отзыв клиента, строго
следуя гайду тона компании ниже. Черновик увидит менеджер клуба и либо опубликует как
есть, либо подправит — ты не публикуешь ничего сам.

=== ГАЙД ТОНА КОМПАНИИ ===
{tone_guide}
=== КОНЕЦ ГАЙДА ===

Обращайся к автору по переданному имени ("Уважаемый(ая) {{имя}}"). Если имя не указано —
не выдумывай его, обратись без имени ("Уважаемый(ая) клиент" или начни без обращения).

Если отзыв позитивный — review_type="positive", просто тёплый благодарный ответ по
структуре из гайда.

Если отзыв негативный — определи review_type СТРОГО одним из двух: "конструктив" или
"эмоциональный" (см. таблицу классификации в гайде). НЕ используй "чёрная реклама" или
"троллинг", даже если подозреваешь — вместо этого, если отзыв выглядит явно бессвязным,
провокационным или не по адресу, поставь review_type="требует_проверки" и оставь
reply_draft пустым, а в internal_note объясни подозрение — решение по таким случаям
принимает человек, не ты.

Если передан контекст активных алертов (повторяющаяся жалоба на ту же тему) — упомяни
это ТОЛЬКО в internal_note для менеджера (например, "это уже N-я жалоба на X за 30 дней,
возможно системная проблема"), НЕ в публичном ответе клиенту.

Ответь строго в формате JSON:
{{
  "review_type": "positive|конструктив|эмоциональный|требует_проверки",
  "reply_draft": "текст черновика ответа клиенту, или пустая строка если требует_проверки",
  "internal_note": "заметка для менеджера, или null если нечего добавить"
}}
"""


def load_tone_guide(path: str) -> str:
    with open(PROJECT_ROOT / path, encoding="utf-8") as f:
        return f.read()


def draft_reply(review: dict, tags: list[dict], tone_guide: str, alert_context: list[dict]) -> dict:
    system = SYSTEM_PROMPT.format(tone_guide=tone_guide)

    tags_str = ", ".join(f"{t['tag']}:{t['tag_sentiment']}" for t in tags) or "без тем"
    alert_str = "нет"
    if alert_context:
        alert_str = "; ".join(
            f"тема «{a['tag']}» — {a['severity']}, {a['count_in_window']} жалоб за {a['window_matched']} дн."
            for a in alert_context
        )

    user = (
        f"Имя автора: {review['author'] or 'не указано (не выдумывай имя, обратись без него)'}\n"
        f"Рейтинг: {review['rating']}/5\n"
        f"Текст отзыва: {review['text']}\n"
        f"Тональность (уже определена системой): {review['sentiment']} ({review['sentiment_score']}/10)\n"
        f"Темы: {tags_str}\n"
        f"Срочно (urgency): {'да' if review['urgency'] else 'нет'}\n"
        f"Активные алерты по темам этого отзыва: {alert_str}"
    )

    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)
