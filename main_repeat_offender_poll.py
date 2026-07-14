"""Ловит нажатия кнопки "Связался с клиентом" на repeat-offender уведомлениях
(см. main_repeat_offender_notify.py). Polling (getUpdates), не webhook — нет
долгоживущего сервера в этом проекте, только батч-скрипты по расписанию (см.
PLAN.md). Запускать часто (каждые 5-15 минут, systemd timer) — иначе человек
нажимает кнопку и ничего не происходит до следующего опроса.

Курсор последнего обработанного update_id хранится в clients/<slug>/telegram_offset.json
(per-client — у каждого свой бот, апдейты не пересекаются)."""
import json

from core.env import get_client_root, load_env
from core import db
from agents.notifier import get_updates, answer_callback_query, remove_message_keyboard

load_env()

OFFSET_FILE_NAME = "telegram_offset.json"


def _load_offset() -> int | None:
    path = get_client_root() / OFFSET_FILE_NAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("offset")


def _save_offset(offset: int) -> None:
    path = get_client_root() / OFFSET_FILE_NAME
    path.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def main():
    conn = db.get_connection()
    db.init_db(conn)

    offset = _load_offset()
    updates = get_updates(offset)

    handled = 0
    max_update_id = offset - 1 if offset else None

    for update in updates:
        max_update_id = update["update_id"]
        cq = update.get("callback_query")
        if not cq or not cq.get("data", "").startswith("ro_ack:"):
            continue

        alert_id = int(cq["data"].split(":", 1)[1])
        alert = db.get_alert_by_id(conn, alert_id)

        if alert is None or alert["status"] == "resolved":
            reply_text = "Этот алерт уже закрыт."
        elif alert["status"] == "acknowledged":
            reply_text = "Уже отмечено ранее."
        else:
            who = cq["from"].get("first_name") or cq["from"].get("username") or "неизвестно"
            db.acknowledge_alert(conn, alert_id, who)
            reply_text = "Спасибо, отмечено!"

        # answerCallbackQuery/editMessageReplyMarkup — это только обратная связь в
        # интерфейсе Telegram (всплывающая подсказка, скрытие кнопки), не бизнес-логика.
        # acknowledge_alert выше уже сохранён в БД к этому моменту — падать из-за
        # устаревшего callback_query_id (Telegram даёт на ответ ограниченное окно) или
        # повторного апдейта на то же нажатие нельзя, иначе offset не сохранится и
        # тот же апдейт будет пытаться обработаться заново каждый следующий прогон.
        try:
            answer_callback_query(cq["id"], reply_text)
        except Exception as e:
            print(f"  (не удалось ответить на callback {cq['id']}: {e})")

        message = cq.get("message") or {}
        if message.get("message_id"):
            try:
                remove_message_keyboard(message["chat"]["id"], message["message_id"])
            except Exception as e:
                print(f"  (не удалось убрать кнопку у сообщения {message['message_id']}: {e})")
        handled += 1

    if max_update_id is not None:
        _save_offset(max_update_id + 1)

    print(f"Обработано нажатий: {handled}")
    conn.close()


if __name__ == "__main__":
    main()
