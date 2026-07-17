"""Пустой файл — присутствие conftest.py в корне заставляет pytest добавить эту
папку в sys.path, чтобы тесты в tests/ могли делать `from core import db`,
`from agents.alert_engine import ...`, `import main_analyze` и т.п. без установки
пакета (уточнено 2026-07-17 — импорты давно ушли от плоской раскладки `import db`)."""
