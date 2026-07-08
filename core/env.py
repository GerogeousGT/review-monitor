"""Единая загрузка .env и путей клиента. Multi-tenant: один код обслуживает
несколько клиентов (clients/<slug>/), выбор клиента — через переменную окружения
CLIENT_SLUG (обязательна, чтобы никогда не запуститься "не на того" клиента молча).

PROJECT_ROOT — корень репозитория (общий код, venv).
CLIENT_ROOT — clients/<slug>/ (свой client_config.yaml, tone_of_voice.md, db/, .env
с TELEGRAM_BOT_TOKEN/CHAT_ID — разные боты на разных клиентов).

.env грузится в порядке: свой clients/<slug>/.env (TELEGRAM_BOT_TOKEN/CHAT_ID,
APIFY_API_TOKEN — разные боты и разный Apify-биллинг на разных клиентов) → корневой
.env (fallback для ключей, реально общих на всех клиентов, напр. PROXYAPI_KEY, если
один аккаунт LLM-провайдера на все проекты) → c:\\ClaudeCode\\AI\\.env как последний fallback.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _client_slug() -> str:
    slug = os.environ.get("CLIENT_SLUG")
    if not slug:
        raise RuntimeError(
            "CLIENT_SLUG не задан — укажи клиента явно (например CLIENT_SLUG=worldclass), "
            "чтобы случайно не запустить пайплайн не на той конфигурации/БД."
        )
    return slug


def get_client_root() -> Path:
    root = PROJECT_ROOT / "clients" / _client_slug()
    if not root.is_dir():
        raise RuntimeError(f"Клиент '{_client_slug()}' не найден: {root} не существует.")
    return root


def load_env() -> None:
    load_dotenv(dotenv_path=get_client_root() / ".env")
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
    load_dotenv(dotenv_path=PROJECT_ROOT.parent.parent / ".env")
