"""Единая загрузка .env — сначала свой (для будущего самостоятельного деплоя на VPS),
затем общий c:\\ClaudeCode\\AI\\.env как fallback (переиспользуем ключи, которых нет
в своём .env, например GROQ_API_KEY). PROJECT_ROOT — корень review-monitor (на уровень
выше этого файла, т.к. он лежит в core/) — используют и config.py, и db.py, чтобы пути
к client_config.yaml/db/reviews.db не зависели от того, откуда запущен скрипт."""
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
    load_dotenv(dotenv_path=PROJECT_ROOT.parent.parent / ".env")
