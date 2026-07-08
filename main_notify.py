"""Цикл уведомлений (запускается каждые 6ч): новые отзывы → изменения алертов.
Просрочки по SLA вынесены в main_watchdog.py (суточный запуск) и main_weekly_stale.py
(недельный) — иначе один и тот же старый "хвост" слался бы заново каждые 6 часов.
Ничего не постит на площадки — только шлёт в Telegram, решение и ответ всегда за человеком."""
from core import db
from agents.alert_engine import recompute_all
from core.config import load_config
from agents.notifier import (
    send_message, format_review_message, format_alert_message,
    format_resolved_message,
)


def main():
    cfg = load_config()
    conn = db.get_connection()
    db.init_db(conn)

    sent = 0

    for review in db.get_unnotified_reviews(conn):
        tags = db.get_review_tags(conn, review["id"])
        location_name = db.get_location_name(conn, review["location_id"])
        send_message(format_review_message(dict(review), tags, location_name))
        db.mark_notified(conn, review["id"])
        sent += 1

    for change in recompute_all(conn, cfg, db):
        location_name = db.get_location_name(conn, change["location_id"])
        if change["action"] == "resolved_auto":
            send_message(format_resolved_message(change, location_name))
        else:
            send_message(format_alert_message(change, location_name))
        sent += 1

    print(f"Отправлено сообщений: {sent}")
    conn.close()


if __name__ == "__main__":
    main()
