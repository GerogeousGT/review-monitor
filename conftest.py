"""Пустой файл — присутствие conftest.py в корне заставляет pytest добавить эту
папку в sys.path, чтобы тесты в tests/ могли делать `import db`, `import alert_engine`
и т.п. без установки пакета."""
