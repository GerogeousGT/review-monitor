"""Единый парсер дат отзывов — площадки отдают разные форматы (Z-суффикс, +03:00,
+07:00), а Apify/2ГИС иногда присылает дробные секунды не из 6 цифр (например
".82112" вместо ".821120"), что Python 3.10 не парсит через datetime.fromisoformat
(в 3.11+ парсер терпимее — баг всплыл только на VPS, не на локальной машине)."""
import re
from datetime import datetime, timezone

_FRACTION_RE = re.compile(r"\.(\d+)")


def parse_review_date(iso: str) -> datetime:
    iso = iso.replace("Z", "+00:00")

    def _pad(match: re.Match) -> str:
        return "." + match.group(1).ljust(6, "0")[:6]

    iso = _FRACTION_RE.sub(_pad, iso)
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
