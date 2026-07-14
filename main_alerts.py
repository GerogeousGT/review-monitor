"""Пересчитывает алерты по тегам и по повторным негативщикам, проверяет просроченные
по SLA ответы. Без LLM, без Telegram — только счёт и запись состояния; отправка
уведомлений будет в Notifier (repeat offender — отдельно, см. main_repeat_offender_notify.py)."""
from core import db
from agents.alert_engine import recompute_all, recompute_repeat_offenders
from core.config import load_config

SEVERITY_ICON = {"yellow": "🟡", "red": "🔴", "resolved_auto": "🟢"}


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
