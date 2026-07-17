"""Разрешение периода для графика динамики тональности (дашборд, 2026-07-17) —
чистая логика дат/диапазонов, без Flask. Вынесено из app.py отдельным модулем
по той же причине, что и webapp/charts.py: app.py тянет Flask/flask_login,
которых нет в корневом requirements.txt (webapp/ живёт в своём изолированном
venv на VPS, см. README "Установка"), а эта логика должна оставаться
тестируемой из корневого venv без webapp-зависимостей — deploy.sh гоняет
`pytest tests/ -x` именно корневым venv, и импорт всего app.py оборвал бы
деплой ModuleNotFoundError на Flask."""
from datetime import datetime, timedelta, timezone

# Гранулярность подобрана так, чтобы число баров оставалось читаемым (не 90
# дневных полосок на графике шириной с контентную колонку): неделя/месяц — по
# дням (7/30 баров), квартал — по неделям (~13 баров).
PERIOD_PRESETS = {
    "week": {"title": "Неделя", "days": 7, "granularity": "day"},
    "month": {"title": "Месяц", "days": 30, "granularity": "day"},
    "quarter": {"title": "Квартал", "days": 90, "granularity": "week"},
}
DEFAULT_PERIOD = "month"

MAX_CUSTOM_RANGE_DAYS = 730  # ~2 года — защита от абсурдно широкого диапазона в URL


def auto_granularity(since_dt: datetime, until_dt: datetime) -> str:
    """Гранулярность для ПРОИЗВОЛЬНОГО диапазона (свой диапазон дат) — у
    пресетов гранулярность фиксирована в PERIOD_PRESETS, но у custom-диапазона
    длина заранее не известна: 3 года по дням дали бы ~1000 баров. Пороги те
    же по духу, что у пресетов: до месяца — по дням, до полугода — по неделям,
    дальше — по месяцам."""
    span_days = (until_dt - since_dt).days
    if span_days <= 35:
        return "day"
    if span_days <= 180:
        return "week"
    return "month"


def resolve_period(
    period_key: str, custom_since: str | None = None, custom_until: str | None = None
) -> tuple[str, str, str, str]:
    """period_key -> (since_iso, until_iso, granularity, effective_period_key).

    effective_period_key может отличаться от запрошенного period_key: если
    period_key='custom', но даты кривые/отсутствуют — функция тихо
    откатывается на DEFAULT_PERIOD (это параметр страницы дашборда, не
    API-контракт — опечатка в URL не должна ронять страницу 400-й ошибкой), и
    возвращает ЭТОТ фактический ключ, а не запрошенный "custom" — иначе
    шаблон подсветил бы вкладку "Свой диапазон" активной, хотя реально показан
    дефолтный месяц (несоответствие вкладки и данных на экране).

    period_key='custom' — свой диапазон дат: custom_since/custom_until —
    строки 'YYYY-MM-DD' из <input type="date">. Любая нестыковка (кривой
    формат, пустое поле, until в будущем, since > until, диапазон шире
    MAX_CUSTOM_RANGE_DAYS) — тот же тихий откат."""
    now = datetime.now(timezone.utc)

    if period_key == "custom" and custom_since and custom_until:
        try:
            since_dt = datetime.strptime(custom_since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            # until включает весь указанный день целиком (23:59:59), не полночь начала дня
            until_dt = datetime.strptime(custom_until, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1, seconds=-1)
        except ValueError:
            pass  # кривой формат даты — падаем ниже на дефолтный пресет
        else:
            if until_dt > now:
                until_dt = now
            if since_dt > until_dt:
                since_dt, until_dt = until_dt, since_dt  # defensive swap, не ошибка страницы
            if until_dt - since_dt > timedelta(days=MAX_CUSTOM_RANGE_DAYS):
                since_dt = until_dt - timedelta(days=MAX_CUSTOM_RANGE_DAYS)
            return since_dt.isoformat(), until_dt.isoformat(), auto_granularity(since_dt, until_dt), "custom"

    effective_key = period_key if period_key in PERIOD_PRESETS else DEFAULT_PERIOD
    preset = PERIOD_PRESETS[effective_key]
    since = (now - timedelta(days=preset["days"])).isoformat()
    return since, now.isoformat(), preset["granularity"], effective_key
