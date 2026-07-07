import hashlib

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def synthetic_id(*parts: str) -> str:
    """ID для площадок, где нет собственного стабильного ID отзыва —
    хэш от автора+даты+начала текста, стабилен между запусками."""
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
