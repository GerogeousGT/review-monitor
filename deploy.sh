#!/usr/bin/env bash
# Деплой на VPS: git pull + рестарт webapp (единственный постоянно работающий
# процесс — batch main_*.py сами подхватят новый код на следующем запуске по
# таймеру, их рестартовать не нужно). БД (clients/*/db/) никогда не деплоится —
# в .gitignore, живёт только на VPS.
#
# Запускать ЛОКАЛЬНО, после git push:
#   ./deploy.sh
#
# Предполагает SSH-алиас "vps-deploy" (см. ~/.ssh/config) с доступом к
# /home/deploy/bots/review-monitor на сервере.
set -euo pipefail

REMOTE_HOST="vps-deploy"
REMOTE_DIR="/home/deploy/bots/review-monitor"

echo "==> Деплой на VPS ($REMOTE_HOST:$REMOTE_DIR)"

ssh "$REMOTE_HOST" "cd '$REMOTE_DIR' && \
  git fetch origin -q && \
  git status --short && \
  git pull origin master && \
  echo '--- requirements.txt ---' && \
  .venv/bin/pip install -q -r requirements.txt && \
  echo '--- webapp/requirements.txt (отдельный venv, см. CHANGELOG 2026-07-16) ---' && \
  webapp/.venv/bin/pip install -q -r webapp/requirements.txt && \
  echo '--- pytest ---' && \
  CLIENT_SLUG=worldclass .venv/bin/python -m pytest tests/ -x -q && \
  echo '--- рестарт webapp ---' && \
  sudo systemctl restart review-monitor-webapp.service && \
  sleep 1 && \
  systemctl is-active review-monitor-webapp.service"

echo "==> Готово"
