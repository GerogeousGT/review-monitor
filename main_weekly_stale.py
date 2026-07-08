"""Недельный обзор давних неотвеченных отзывов: старше watchdog-окна
(watchdog_recent_days), но моложе полного списания (watchdog_stale_cutoff_days) —
см. core/config.py. Отзывы старше stale_cutoff уже не показываются нигде,
но остаются в БД как reply_status='pending' для статистики в финальном дашборде."""
from core import db
from core.config import load_config
from agents.notifier import send_message, format_weekly_stale_message


def main():
    cfg = load_config()
    recent_days = cfg["collection"]["watchdog_recent_days"]
    stale_cutoff_days = cfg["collection"]["watchdog_stale_cutoff_days"]

    conn = db.get_connection()
    db.init_db(conn)

    stale_by_location: dict[str, list[dict]] = {}
    for r in db.get_stale_overdue_reviews(conn, recent_cutoff_days=recent_days, stale_cutoff_days=stale_cutoff_days):
        stale_by_location.setdefault(r["location_id"], []).append(dict(r))

    sent = 0
    for location_id, stale in stale_by_location.items():
        location_name = db.get_location_name(conn, location_id)
        send_message(format_weekly_stale_message(stale, location_name))
        sent += 1

    print(f"Отправлено сообщений: {sent}")
    conn.close()


if __name__ == "__main__":
    main()
