import yaml

from .env import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "client_config.yaml"


def load_config(path=CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
