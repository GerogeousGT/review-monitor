"""Отправка сообщений в Telegram. HTML parse_mode (не MarkdownV2) — у HTML всего три
спецсимвола для экранирования (& < >), с MarkdownV2 на произвольном пользовательском
тексте отзывов (звёздочки, подчёркивания) было бы минное поле. Автопостинга ответов
на площадки нет — только уведомление человеку, решение всегда за ним.
"""
import html as html_lib
import os

import requests

from core.env import load_env

load_env()

API_BASE = "https://api.telegram.org/bot{token}/{method}"

PLATFORM_LABEL = {"yandex_maps": "Я.Карты", "zoon": "Zoon", "2gis": "2ГИС"}
SENTIMENT_ICON = {"positive": "🟢", "neutral": "🟡", "negative": "🔴"}
SEVERITY_ICON = {"yellow": "🟡", "red": "🔴"}


def _esc(s) -> str:
    return html_lib.escape(str(s or ""))


def send_message(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = API_BASE.format(token=token, method="sendMessage")
    resp = requests.post(
        url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {data}")


def format_review_message(review: dict, tags: list[dict], location_name: str) -> str:
    stars = "★" * (review["rating"] or 0) + "☆" * (5 - (review["rating"] or 0))
    platform = PLATFORM_LABEL.get(review["platform"], review["platform"])
    sentiment_dot = SENTIMENT_ICON.get(review["sentiment"], "")

    lines = [
        f"{'🚩 <b>СРОЧНО</b> — ' if review['urgency'] else ''}Новый отзыв — <b>{_esc(location_name)}</b>",
        f"{platform} · {stars} · {_esc(review['author'] or 'без имени')}",
        "",
        _esc(review["text"] or "(без текста)"),
        "",
        f"<b>Тональность:</b> {sentiment_dot} {review['sentiment']} ({review['sentiment_score']}/10)",
        f"<b>Почему:</b> {_esc(review['sentiment_reasoning'])}",
    ]

    if tags:
        lines.append("<b>Темы:</b>")
        for t in tags:
            dot = SENTIMENT_ICON.get(t["tag_sentiment"], "")
            lines.append(f"{dot} <b>{_esc(t['tag'])}</b>")

    if review.get("review_type") == "требует_проверки":
        lines += ["", "⚠️ <b>Требует ручной проверки</b> — черновик не собран, см. заметку ниже"]
    elif review.get("reply_draft"):
        lines += ["", "<b>Черновик ответа:</b>", _esc(review["reply_draft"])]

    if review.get("internal_note"):
        lines += ["", f"<b>Заметка для менеджера:</b> {_esc(review['internal_note'])}"]

    return "\n".join(lines)


def format_alert_message(change: dict, location_name: str) -> str:
    icon = SEVERITY_ICON.get(change["severity"], "•")
    if change["action"] == "opened":
        return (
            f"{icon} Новый алерт — <b>{_esc(location_name)}</b>\n"
            f"Тема: <b>{_esc(change['tag'])}</b> — {change['count_in_window']} негативных отзывов "
            f"за {change['window_matched']} дн. ({change['severity']})"
        )
    return (
        f"{icon} Алерт обновлён — <b>{_esc(location_name)}</b>\n"
        f"Тема: <b>{_esc(change['tag'])}</b> — {change['previous_severity']} → {change['severity']} "
        f"({change['count_in_window']} за {change['window_matched']} дн.)"
    )


def format_resolved_message(change: dict, location_name: str) -> str:
    return f"🟢 Алерт закрыт — <b>{_esc(location_name)}</b>\nТема: <b>{_esc(change['tag'])}</b> — счёт вернулся в норму."


def format_digest_message(location_name: str, sentiment_counts: dict, active_alerts: list[dict], overdue_count: int) -> str:
    """Ежедневная сводка (digest_time в конфиге) — в отличие от разовых уведомлений
    показывает ВСЕ незакрытые алерты (не только изменившиеся за сутки), чтобы про
    открытую проблему не забыли, даже если по ней давно не было новых жалоб."""
    total = sum(sentiment_counts.values())
    lines = [
        f"📋 <b>Дневная сводка — {_esc(location_name)}</b>",
        "",
        f"Новых отзывов за сутки: {total} "
        f"(🟢 {sentiment_counts.get('positive', 0)} · 🟡 {sentiment_counts.get('neutral', 0)} · 🔴 {sentiment_counts.get('negative', 0)})",
    ]

    if active_alerts:
        lines.append("")
        lines.append(f"<b>Открытые алерты ({len(active_alerts)}):</b>")
        for a in active_alerts:
            icon = SEVERITY_ICON.get(a["severity"], "•")
            ack = " (в работе)" if a["status"] == "acknowledged" else ""
            lines.append(
                f"{icon} #{a['id']} <b>{_esc(a['tag'])}</b> — {a['count_in_window']} за {a['window_matched']} дн.{ack}"
            )
    else:
        lines.append("\n🟢 Открытых алертов нет.")

    lines.append("")
    lines.append(f"⏰ Просрочено по SLA ответа: {overdue_count}")

    return "\n".join(lines)


def format_watchdog_message(overdue: list[dict], location_name: str) -> str:
    lines = [f"⏰ Просрочен ответ (<b>{_esc(location_name)}</b>) — {len(overdue)} отзыв(ов):"]
    for r in overdue:
        platform = PLATFORM_LABEL.get(r["platform"], r["platform"])
        sentiment_dot = SENTIMENT_ICON.get(r["sentiment"], "")
        lines.append(
            f"  • #{r['id']} {platform}, {sentiment_dot} {r['sentiment']} — дедлайн был {r['reply_sla_deadline'][:10]}"
        )
    return "\n".join(lines)


def format_weekly_summary_message(
    location_name: str,
    week_counts: dict,
    month_counts: dict,
    top_positive_month: list[dict],
    top_negative_month: list[dict],
    active_alerts: list[dict],
    overdue_count: int,
) -> str:
    """Еженедельная сводка (main_weekly_summary.py, понедельник 08:00 МСК) — отвечает
    на вопрос руководителя "что делать с этой статистикой", не просто "что обсуждали":
    объём неделя+месяц (неделя одна часто слишком мала, чтобы видеть тренд), топ
    хвалимого и топ ругаемого ОТДЕЛЬНО (за месяц — за неделю почти всегда шум из
    1-2 упоминаний), и то, что уже прошло порог "системной проблемы" в Alert Engine —
    не отдельный расчёт, а прямой список активных yellow/red алертов."""
    week_total = sum(week_counts.values())
    month_total = sum(month_counts.values())
    lines = [
        f"🗓️ <b>Итоги недели — {_esc(location_name)}</b>",
        "",
        f"Отзывов за неделю: {week_total} "
        f"(🟢 {week_counts.get('positive', 0)} · 🟡 {week_counts.get('neutral', 0)} · 🔴 {week_counts.get('negative', 0)})",
        f"Отзывов за месяц: {month_total} "
        f"(🟢 {month_counts.get('positive', 0)} · 🟡 {month_counts.get('neutral', 0)} · 🔴 {month_counts.get('negative', 0)})",
    ]

    lines.append("")
    if top_positive_month:
        lines.append("<b>Чаще всего хвалят (за месяц):</b>")
        for t in top_positive_month:
            lines.append(f"  🟢 <b>{_esc(t['tag'])}</b> — {t['count']}")
    else:
        lines.append("Чаще всего хвалят: недостаточно данных за месяц.")

    lines.append("")
    if top_negative_month:
        lines.append("<b>Чаще всего ругают (за месяц):</b>")
        for t in top_negative_month:
            lines.append(f"  🔴 <b>{_esc(t['tag'])}</b> — {t['count']}")
    else:
        lines.append("Чаще всего ругают: недостаточно данных за месяц.")

    lines.append("")
    if active_alerts:
        lines.append(f"<b>Системный негатив (открытые алерты, {len(active_alerts)}):</b>")
        for a in active_alerts:
            icon = SEVERITY_ICON.get(a["severity"], "•")
            ack = " (в работе)" if a["status"] == "acknowledged" else ""
            lines.append(
                f"  {icon} <b>{_esc(a['tag'])}</b> — {a['count_in_window']} за {a['window_matched']} дн.{ack}"
            )
    else:
        lines.append("🟢 Системного негатива нет — открытых алертов нет.")

    lines.append("")
    lines.append(f"⏰ Просрочено по SLA за неделю: {overdue_count}")

    return "\n".join(lines)


def format_weekly_stale_message(stale: list[dict], location_name: str) -> str:
    """Раз в неделю (main_weekly_stale.py) — отзывы, просроченные дольше watchdog-окна,
    но ещё не списанные совсем. Без срочного тона: отвечать уже не горит, но забывать
    про них рано (полная статистика по возрасту неотвеченных — в финальном дашборде)."""
    lines = [
        f"📅 Давние неотвеченные (<b>{_esc(location_name)}</b>) — {len(stale)} отзыв(ов), "
        f"без срочности, но ещё не списаны:"
    ]
    for r in stale:
        platform = PLATFORM_LABEL.get(r["platform"], r["platform"])
        sentiment_dot = SENTIMENT_ICON.get(r["sentiment"], "")
        lines.append(
            f"  • #{r['id']} {platform}, {sentiment_dot} {r['sentiment']} — дедлайн был {r['reply_sla_deadline'][:10]}"
        )
    return "\n".join(lines)
