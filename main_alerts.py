"""Пересчитывает алерты по тегам, зонам и повторным негативщикам, проверяет
просроченные по SLA ответы, и ШЛЁТ в Telegram уведомления об изменениях
тег/зона-алертов (тонкий пинг "иди в дашборд").

Почему отправка здесь, а не в main_notify.py (исправлено 2026-07-17): recompute_*
одновременно СЧИТАЕТ и ЗАПИСЫВАЕТ состояние алертов. Раньше main_alerts.py
пересчитывал (применял изменения, только печатал их), а main_notify.py секундой
позже в том же run_cycle.sh пересчитывал ПОВТОРНО — состояние уже актуально, diff
пустой, ни одно сообщение об алерте в Telegram не уходило (проверено: idempotency
recompute_all + ExecStart=run_cycle.sh на VPS). Уведомление должно жить там же, где
единственный пересчёт — здесь. main_notify.py больше алерты не трогает (только
карточки новых отзывов). Repeat offender по-прежнему уведомляется отдельным
таймером (main_repeat_offender_notify.py), в этот пинг не входит."""
from core import db
from agents.alert_engine import recompute_all, recompute_repeat_offenders, recompute_zone_alerts
from agents.notifier import (
    send_message,
    format_alert_message,
    format_resolved_message,
    format_zone_alert_message,
    format_zone_resolved_message,
)
from core.config import load_config

SEVERITY_ICON = {"yellow": "🟡", "red": "🔴", "resolved_auto": "🟢"}


def send_alert_changes(conn, tag_changes: list[dict], zone_changes: list[dict], notify=send_message) -> int:
    """Шлёт по одному тонкому пингу на каждое изменение тег/зона-алерта (open/update/
    resolve). notify инъектируется для теста (по умолчанию — реальный Telegram).
    Каждая отправка обёрнута в try/except: сбой доставки одного сообщения (напр.
    временная ошибка Telegram) не должен ронять весь run_cycle.sh (set -e) и мешать
    остальным. Состояние алерта уже записано recompute_* до вызова этой функции."""
    sent = 0
    for c in tag_changes:
        location_name = db.get_location_name(conn, c["location_id"])
        text = format_resolved_message(c, location_name) if c["action"] == "resolved_auto" else format_alert_message(c, location_name)
        try:
            notify(text)
            sent += 1
        except Exception as e:
            print(f"[alert notify] не удалось отправить тег-алерт '{c['tag']}': {e}")
    for c in zone_changes:
        location_name = db.get_location_name(conn, c["location_id"])
        text = format_zone_resolved_message(c, location_name) if c["action"] == "resolved_auto" else format_zone_alert_message(c, location_name)
        try:
            notify(text)
            sent += 1
        except Exception as e:
            print(f"[alert notify] не удалось отправить зона-алерт '{c['zone']}': {e}")
    return sent


def main():
    cfg = load_config()
    conn = db.get_connection()
    db.init_db(conn)

    changes = recompute_all(conn, cfg, db)
    if not changes:
        print("Изменений в алертах нет.")
    for c in changes:
        icon = SEVERITY_ICON.get(c["action"] if c["action"] == "resolved_auto" else c["severity"], "•")
        if c["action"] == "resolved_auto":
            print(f"{icon} [{c['location_id']}] '{c['tag']}' — закрыт (счёт вернулся в норму)")
        elif c["action"] == "opened":
            print(f"{icon} [{c['location_id']}] '{c['tag']}' — НОВЫЙ алерт {c['severity']} "
                  f"({c['count_in_window']} за {c['window_matched']} дн.)")
        else:
            print(f"{icon} [{c['location_id']}] '{c['tag']}' — обновлён {c['previous_severity']} → {c['severity']} "
                  f"({c['count_in_window']} за {c['window_matched']} дн.)")

    zone_changes = recompute_zone_alerts(conn, cfg, db)
    if zone_changes:
        print("\nЗональные алерты:")
    for c in zone_changes:
        icon = SEVERITY_ICON.get(c["action"] if c["action"] == "resolved_auto" else c["severity"], "•")
        if c["action"] == "resolved_auto":
            print(f"{icon} [{c['location_id']}] зона '{c['zone']}' — закрыт (счёт вернулся в норму)")
        elif c["action"] == "opened":
            print(f"{icon} [{c['location_id']}] зона '{c['zone']}' — НОВЫЙ алерт {c['severity']} "
                  f"({c['count_in_window']} за {c['window_matched']} дн.)")
        else:
            print(f"{icon} [{c['location_id']}] зона '{c['zone']}' — обновлён {c['previous_severity']} → {c['severity']} "
                  f"({c['count_in_window']} за {c['window_matched']} дн.)")

    # Тонкий пинг в Telegram про изменения тег/зона-алертов — ЕДИНСТВЕННОЕ место
    # отправки алертов в цикле (см. docstring модуля). Repeat offender — отдельным
    # таймером, сюда не входит.
    notified = send_alert_changes(conn, changes, zone_changes)
    print(f"\nОтправлено алерт-пингов в Telegram: {notified}")

    offender_changes = recompute_repeat_offenders(conn, cfg, db)
    if offender_changes:
        print("\nПовторные негативщики:")
    for c in offender_changes:
        if c["action"] == "resolved_auto":
            print(f"🟢 [{c['location_id']}] {c['author']} — закрыт (вышел из окна)")
        elif c["action"] == "opened":
            print(f"🟡 [{c['location_id']}] {c['author']} — НОВЫЙ алерт ({c['count_in_window']} негативных отзывов)")
        else:
            print(f"🟡 [{c['location_id']}] {c['author']} — обновлён ({c['count_in_window']} негативных отзывов)")

    overdue = db.get_overdue_reviews(conn)
    print(f"\nПросрочены по SLA ответа: {len(overdue)}")
    for r in overdue:
        print(f"  🚩 review #{r['id']} ({r['platform']}, {r['sentiment']}) — дедлайн был {r['reply_sla_deadline']}")

    conn.close()


if __name__ == "__main__":
    main()
