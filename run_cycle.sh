#!/bin/bash
# Один цикл пайплайна для одного клиента: Collector -> Sentiment Analyst -> Reply Strategist -> Alert Engine -> Notifier.
# Останавливается на первой ошибке, чтобы не слать уведомления по неполным данным.
# Использование: ./run_cycle.sh <client_slug>, например ./run_cycle.sh worldclass
set -e
cd "$(dirname "$0")"

if [ -z "$1" ]; then
  echo "Использование: $0 <client_slug> (например worldclass, daudelsport)" >&2
  exit 1
fi
export CLIENT_SLUG="$1"

.venv/bin/python main_collect.py
.venv/bin/python main_analyze.py
.venv/bin/python main_reply.py
.venv/bin/python main_alerts.py
.venv/bin/python main_notify.py
