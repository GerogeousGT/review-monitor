"""Alert Engine — считает статистику по тегам и держит персистентное состояние тревоги.

Никакого LLM: подсчёт окон — обычный код, чтобы цифры были проверяемые и без риска,
что модель "придумает" статистику вместо честного счёта (см. CHANGELOG).
"""
from datetime import datetime, timedelta, timezone

from core.dates import parse_review_date as _parse_date


def _rules_for_tag(tag: str, alert_rules_cfg: dict) -> list[dict]:
    overrides = alert_rules_cfg.get("overrides") or {}
    return overrides.get(tag, alert_rules_cfg["default"])


def _severity_for_count(count: int, rule: dict) -> str:
    if count >= rule["red_at"]:
        return "red"
    if count >= rule["yellow_at"]:
        return "yellow"
    return "green"


_SEVERITY_RANK = {"green": 0, "yellow": 1, "red": 2}


def _count_in_window(events: list[dict], window_days: int, now: datetime) -> int:
    cutoff = now - timedelta(days=window_days)
    review_ids = {e["review_id"] for e in events if _parse_date(e["review_date"]) >= cutoff}
    return len(review_ids)  # DISTINCT review_id — один отзыв с двумя упоминаниями темы не считается дважды


def compute_burst_severity(events: list[dict], tag: str, alert_rules_cfg: dict, now: datetime) -> dict:
    """"Вспышка" — несколько окон проверяются НЕЗАВИСИМО, берём худший результат.
    Ловит резкий всплеск жалоб за короткий/средний срок (по умолчанию 30/90 дней)."""
    rules = _rules_for_tag(tag, alert_rules_cfg)
    best = None

    for rule in rules:
        count = _count_in_window(events, rule["window_days"], now)
        severity = _severity_for_count(count, rule)
        candidate = {"severity": severity, "window_matched": rule["window_days"], "count_in_window": count}
        # Первое правило всегда задаёт базовый результат (даже если green) — иначе
        # при "все окна зелёные" count_in_window остался бы заглушкой 0 вместо
        # реального счёта по первому окну.
        if best is None or _SEVERITY_RANK[severity] > _SEVERITY_RANK[best["severity"]]:
            best = candidate

    return best


def compute_chronic_severity(events: list[dict], chronic_tiers: list[dict], now: datetime) -> dict:
    """"Тлеющая проблема" — ступенчатая ЭСКАЛАЦИЯ, не параллельная проверка. Каждый
    следующий уровень (360, 720 дней) проверяется, только если сработал предыдущий —
    иначе старый забытый всплеск полугодовой давности мог бы ложно засветиться как
    "хроническая проблема" в окне 720 дней, хотя сейчас всё нормально.

    Первый пройденный уровень (обычно 180 дней) -> yellow ("похоже, тлеет").
    Второй и глубже (360/720) -> red ("подтверждённая хроническая проблема")."""
    if not chronic_tiers:
        return {"severity": "green", "window_matched": None, "count_in_window": 0}

    reached_idx = -1
    reached_count = 0
    for i, tier in enumerate(chronic_tiers):
        count = _count_in_window(events, tier["window_days"], now)
        if count < tier["threshold"]:
            break
        reached_idx = i
        reached_count = count

    if reached_idx == -1:
        return {"severity": "green", "window_matched": None, "count_in_window": 0}

    severity = "yellow" if reached_idx == 0 else "red"
    return {
        "severity": severity,
        "window_matched": chronic_tiers[reached_idx]["window_days"],
        "count_in_window": reached_count,
    }


def compute_tag_severity(events: list[dict], tag: str, alert_rules_cfg: dict, now: datetime) -> dict:
    """events уже отфильтрованы по точке вызывающей стороной (см. recompute_all) —
    здесь только тег имеет значение. Комбинирует "вспышку" (burst) и "тлеющую проблему"
    (chronic) — берёт худший результат из двух независимых сигналов."""
    burst = compute_burst_severity(events, tag, alert_rules_cfg, now)
    chronic = compute_chronic_severity(events, alert_rules_cfg.get("chronic_tiers") or [], now)

    return chronic if _SEVERITY_RANK[chronic["severity"]] > _SEVERITY_RANK[burst["severity"]] else burst


def recompute_all(conn, cfg, db) -> list[dict]:
    """Проходит по всем (тег, точка), где были негативные упоминания, пересчитывает
    severity и обновляет/создаёт/закрывает алерты. Возвращает список изменений для лога."""
    now = datetime.now(timezone.utc)
    events = db.get_negative_tag_events(conn)
    alert_rules_cfg = cfg["alert_rules"]

    by_tag_location: dict[tuple[str, str], list[dict]] = {}
    for e in events:
        by_tag_location.setdefault((e["tag"], e["location_id"]), []).append(e)

    changes = []
    for (tag, location_id), tag_events in by_tag_location.items():
        result = compute_tag_severity(tag_events, tag, alert_rules_cfg, now)
        existing = db.get_active_alert(conn, tag, location_id)

        if result["severity"] == "green":
            if existing:
                db.resolve_alert(conn, existing["id"])
                changes.append({"tag": tag, "location_id": location_id, "action": "resolved_auto", **result})
            continue

        if existing is None:
            db.create_alert(conn, tag, location_id, result["severity"], result["window_matched"], result["count_in_window"])
            changes.append({"tag": tag, "location_id": location_id, "action": "opened", **result})
        elif existing["severity"] != result["severity"] or existing["count_in_window"] != result["count_in_window"]:
            db.update_alert_severity(conn, existing["id"], result["severity"], result["window_matched"], result["count_in_window"])
            changes.append({"tag": tag, "location_id": location_id, "action": "updated", "previous_severity": existing["severity"], **result})

    return changes
