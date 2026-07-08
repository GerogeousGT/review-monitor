import yaml

from .env import get_client_root


def load_config(path=None) -> dict:
    path = path or (get_client_root() / "client_config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
