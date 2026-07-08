"""Еженедельная сводка — итоги для руководителя (запускать по понедельникам, systemd
timer). В отличие от main_digest.py (снимок текущего состояния "прямо сейчас") — это
объём/тренд за неделю и месяц, топ хвалимого и топ ругаемого раздельно, и системный
негатив (активные алерты Alert Engine, не отдельный повторный расчёт)."""
from datetime import datetime, timedelta, timezone

from core import db
from agents.notifier import send_message, format_weekly_summary_message


def main():
    conn = db.get_connection()
    db.init_db(conn)

    now = datetime.now(timezone.utc)
    since_week = (now - timedelta(days=7)).isoformat()
    since_month = (now - timedelta(days=30)).isoformat()

    all_alerts = [dict(a) for a in db.get_all_active_alerts(conn)]

    for location_id in db.get_locations(conn):
        location_name = db.get_location_name(conn, location_id)
        week_counts = db.get_review_sentiment_counts_since(conn, location_id, since_week)
        month_counts = db.get_review_sentiment_counts_since(conn, location_id, since_month)
        top_positive = db.get_top_tags_by_sentiment_since(conn, location_id, since_month, "positive")
        top_negative = db.get_top_tags_by_sentiment_since(conn, location_id, since_month, "negative")
        location_alerts = [a for a in all_alerts if a["location_id"] == location_id]
        overdue = db.get_overdue_reviews_since(conn, location_id, since_week)

        send_message(format_weekly_summary_message(
            location_name, week_counts, month_counts, top_positive, top_negative, location_alerts, len(overdue)
        ))
        print(f"[{location_id}] недельная сводка отправлена: {sum(week_counts.values())} отзывов за неделю, "
              f"{sum(month_counts.values())} за месяц, {len(location_alerts)} активных алертов, {len(overdue)} просрочено")

    conn.close()


if __name__ == "__main__":
    main()
