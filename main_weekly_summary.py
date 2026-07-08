"""Еженедельная сводка — итоги прошедшей недели (запускать по понедельникам, systemd
timer). В отличие от main_digest.py (снимок текущего состояния "прямо сейчас") — это
динамика ЗА ПЕРИОД: сколько отзывов пришло, топ тем, что случилось с алертами,
сколько просрочек за неделю."""
from datetime import datetime, timedelta, timezone

from core import db
from agents.notifier import send_message, format_weekly_summary_message


def main():
    conn = db.get_connection()
    db.init_db(conn)

    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    for location_id in db.get_locations(conn):
        location_name = db.get_location_name(conn, location_id)
        counts = db.get_review_sentiment_counts_since(conn, location_id, since)
        top_tags = db.get_top_tags_since(conn, location_id, since)
        alerts_opened = [dict(a) for a in db.get_alerts_opened_since(conn, location_id, since)]
        alerts_resolved = [dict(a) for a in db.get_alerts_resolved_since(conn, location_id, since)]
        overdue = db.get_overdue_reviews_since(conn, location_id, since)

        send_message(format_weekly_summary_message(
            location_name, counts, top_tags, alerts_opened, alerts_resolved, len(overdue)
        ))
        print(f"[{location_id}] недельная сводка отправлена: {sum(counts.values())} отзывов, "
              f"{len(alerts_opened)} открыто/{len(alerts_resolved)} закрыто алертов, {len(overdue)} просрочено")

    conn.close()


if __name__ == "__main__":
    main()
