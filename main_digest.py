"""Ежедневная сводка — запускать по расписанию в digest_time (client_config.yaml).
Планировщика на VPS пока нет (см. PLAN.md), поэтому пока запускается вручную/по cron отдельно.
В отличие от main_notify.py показывает ВСЕ открытые алерты, не только изменившиеся сегодня —
чтобы забытая проблема не потерялась между разовыми уведомлениями."""
from datetime import datetime, timedelta, timezone

from core import db
from agents.notifier import send_message, format_digest_message


def main():
    conn = db.get_connection()
    db.init_db(conn)

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    all_alerts = [dict(a) for a in db.get_all_active_alerts(conn)]
    overdue = db.get_overdue_reviews(conn)

    for location_id in db.get_locations(conn):
        location_name = db.get_location_name(conn, location_id)
        counts = db.get_review_sentiment_counts_since(conn, since)
        location_alerts = [a for a in all_alerts if a["location_id"] == location_id]
        location_overdue = [r for r in overdue if r["location_id"] == location_id]

        send_message(format_digest_message(location_name, counts, location_alerts, len(location_overdue)))
        print(f"[{location_id}] сводка отправлена: {sum(counts.values())} отзывов, {len(location_alerts)} алертов, {len(location_overdue)} просрочено")

    conn.close()


if __name__ == "__main__":
    main()
