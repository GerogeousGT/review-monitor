"""Постоянно работающий веб-процесс review-monitor — единственный в проекте (весь
остальной код это одноразовые батч-скрипты по расписанию, см. PLAN.md). Здесь будут
жить: страница логина + список клиентов + дашборд (в разработке) и позже webhook для
Telegram inline-кнопок (см. PLAN.md — 2026-07-14, кнопка "Связался" пока polling-версией
не взлетела по UX, нужен именно webhook, ждёт этого процесса).

Сейчас — только health-check, чтобы nginx+DNS+SSL можно было раскатать и проверить
до того, как появится сам дашборд.
"""
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "review-monitor-webapp"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8789)
