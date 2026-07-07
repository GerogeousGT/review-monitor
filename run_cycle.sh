#!/bin/bash
# Один цикл пайплайна: Collector -> Sentiment Analyst -> Reply Strategist -> Alert Engine -> Notifier.
# Останавливается на первой ошибке, чтобы не слать уведомления по неполным данным.
set -e
cd "$(dirname "$0")"

.venv/bin/python main_collect.py
.venv/bin/python main_analyze.py
.venv/bin/python main_reply.py
.venv/bin/python main_alerts.py
.venv/bin/python main_notify.py
