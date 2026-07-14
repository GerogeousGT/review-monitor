"""Повторное напоминание по открытым repeat-offender алертам — раз в N дней
(client_config.yaml -> alert_rules.repeat_offender.renotify_after_days), отдельным
systemd timer, а не в 6-часовом main_notify.py (иначе спамило бы каждые 6 часов, а не
раз в пару дней). Молчит для acknowledged-алертов — человек уже взял в работу.

Нет кнопки "Связался" — получатель это сервисная служба клиента, не технарь, а
main_ack.py (CLI) ей недоступен. Кнопка требует webhook-приёмника (не polling —
polling даёт заметную задержку между нажатием и реакцией, живая проверка 2026-07-14
показала, что это плохой UX), который сознательно отложен как отдельная инфраструктурная
задача (см. PLAN.md). Вместо ack — жёсткий потолок max_notifications: напоминает
несколько раз и замолкает само, не спамит бесконечно.
"""
from core import db
from core.config import load_config
from agents.notifier import send_message, format_repeat_offender_message


def main():
    cfg = load_config()
    rules = cfg["alert_rules"].get("repeat_offender") or {}
    renotify_after_days = rules.get("renotify_after_days")
    max_notifications = rules.get("max_notifications")
    if not renotify_after_days or not max_notifications:
        print("repeat_offender не настроен для этого клиента — нечего слать.")
        return

    conn = db.get_connection()
    db.init_db(conn)

    due = db.get_repeat_offender_alerts_due_for_renotify(conn, renotify_after_days, max_notifications)
    sent = 0
    for alert in due:
        location_name = db.get_location_name(conn, alert["location_id"])
        is_last = alert["notify_count"] + 1 >= max_notifications
        send_message(format_repeat_offender_message(dict(alert), location_name, is_last))
        db.mark_alert_notified(conn, alert["id"])
        sent += 1

    print(f"Отправлено напоминаний: {sent}")
    conn.close()


if __name__ == "__main__":
    main()
