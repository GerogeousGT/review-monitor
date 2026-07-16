"""Регистрирует Telegram webhook на кнопку "Связался с клиентом" (2026-07-16,
заменяет polling — см. CHANGELOG). Запускать ВРУЧНУЮ один раз на каждого клиента
после первого разворота бота (или после смены токена/домена) — не автоматически
при каждом старте webapp, чтобы не дёргать Telegram API лишний раз.

Использование:
  CLIENT_SLUG=<slug> .venv/Scripts/python main_set_webhook.py
"""
import os

from agents.notifier import set_webhook

WEBHOOK_DOMAIN = "https://reviewpulse.ru"


def main():
    slug = os.environ.get("CLIENT_SLUG")
    if not slug:
        raise RuntimeError("CLIENT_SLUG не задан — на какого клиента регистрировать webhook?")

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    webhook_url = f"{WEBHOOK_DOMAIN}/telegram/webhook/{slug}"
    set_webhook(webhook_url, token)
    print(f"Webhook зарегистрирован: {webhook_url}")


if __name__ == "__main__":
    main()
