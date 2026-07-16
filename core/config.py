import yaml

from .env import get_client_root


def load_config(path=None) -> dict:
    path = path or (get_client_root() / "client_config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_few_shot_examples(path=None) -> list[dict]:
    """Необязательный файл — новый клиент без калибровки его просто не имеет,
    промпт Sentiment Analyst работает и без примеров (см. agents/sentiment_analyst.py)."""
    path = path or (get_client_root() / "few_shot_examples.yaml")
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or []
