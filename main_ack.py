"""Ручное подтверждение алерта — без кнопок в Telegram (см. PLAN.md, v2). Пока просто CLI:

  python main_ack.py                 — список открытых алертов с их id
  python main_ack.py 3               — пометить алерт #3 как "acknowledged" (взят в работу)
  python main_ack.py 3 "Жорж"        — то же, с указанием, кто взял в работу
"""
import sys

from core import db


def main():
    conn = db.get_connection()
    db.init_db(conn)

    if len(sys.argv) < 2:
        alerts = db.get_all_active_alerts(conn)
        if not alerts:
            print("Открытых алертов нет.")
            return
        for a in alerts:
            print(f"#{a['id']} [{a['status']}] {a['tag']} ({a['location_id']}) — "
                  f"{a['severity']}, {a['count_in_window']} за {a['window_matched']} дн., "
                  f"с {a['first_triggered_at'][:16]}")
        return

    alert_id = int(sys.argv[1])
    acknowledged_by = sys.argv[2] if len(sys.argv) > 2 else None

    alert = db.get_alert_by_id(conn, alert_id)
    if alert is None:
        print(f"Алерт #{alert_id} не найден.")
        return
    if alert["status"] == "resolved":
        print(f"Алерт #{alert_id} уже закрыт ({alert['resolved_at']}), acknowledge не нужен.")
        return

    db.acknowledge_alert(conn, alert_id, acknowledged_by)
    print(f"Алерт #{alert_id} ('{alert['tag']}') помечен как в работе"
          + (f" ({acknowledged_by})" if acknowledged_by else "") + ".")

    conn.close()


if __name__ == "__main__":
    main()
