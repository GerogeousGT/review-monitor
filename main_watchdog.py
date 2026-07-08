"""Суточный watchdog: отзывы просроченные по SLA и не старше 3 месяцев (см.
core/config.py — watchdog_recent_days). Запускать раз в сутки (systemd timer),
отдельно от 6-часового main_notify.py — иначе один и тот же список слался бы
заново каждый прогон основного цикла."""
from core import db
from core.config import load_config
from agents.notifier import send_message, format_watchdog_message


def main():
    cfg = load_config()
    recent_days = cfg["collection"]["watchdog_recent_days"]

    conn = db.get_connection()
    db.init_db(conn)

    overdue_by_location: dict[str, list[dict]] = {}
    for r in db.get_overdue_reviews(conn, recent_cutoff_days=recent_days):
        overdue_by_location.setdefault(r["location_id"], []).append(dict(r))

    sent = 0
    for location_id, overdue in overdue_by_location.items():
        location_name = db.get_location_name(conn, location_id)
        send_message(format_watchdog_message(overdue, location_name))
        sent += 1

    print(f"Отправлено сообщений: {sent}")
    conn.close()


if __name__ == "__main__":
    main()
