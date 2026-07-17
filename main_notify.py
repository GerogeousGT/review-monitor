"""Цикл уведомлений (запускается каждые 6ч): карточки новых отзывов в Telegram.

Алерты СЮДА НЕ ВХОДЯТ (исправлено 2026-07-17): их пересчёт и отправку делает
main_alerts.py, который в run_cycle.sh запускается прямо перед этим скриптом. Раньше
main_notify.py тоже вызывал recompute_all — но main_alerts.py уже применил все
изменения секундой раньше, поэтому здесь diff был пустым и ни одно сообщение об
алерте не уходило (см. docstring main_alerts.py). Теперь единственное место
пересчёта+отправки алертов — main_alerts.py.

Просрочки по SLA вынесены в main_watchdog.py (суточный) и main_weekly_stale.py
(недельный) — иначе один и тот же старый "хвост" слался бы заново каждые 6 часов.
Ничего не постит на площадки — только шлёт в Telegram, решение и ответ всегда за человеком."""
from core import db
from agents.notifier import send_message, format_review_message


def main():
    conn = db.get_connection()
    db.init_db(conn)

    sent = 0

    for review in db.get_unnotified_reviews(conn):
        tags = db.get_review_tags(conn, review["id"])
        location_name = db.get_location_name(conn, review["location_id"])
        send_message(format_review_message(dict(review), tags, location_name))
        db.mark_notified(conn, review["id"])
        sent += 1

    print(f"Отправлено карточек новых отзывов: {sent}")
    conn.close()


if __name__ == "__main__":
    main()
