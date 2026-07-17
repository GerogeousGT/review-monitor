-- Точки и площадки, которые мониторим
CREATE TABLE locations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT
);

CREATE TABLE location_platforms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id TEXT NOT NULL REFERENCES locations(id),
    platform TEXT NOT NULL,              -- 2gis | yandex_maps | zoon
    url TEXT NOT NULL,
    last_seen_review_id TEXT,            -- пишется после каждого сбора, но пока не читается ни
                                          -- одним коллектором (не курсор с ранним выходом — см.
                                          -- README "Что делает", уточнено 2026-07-17); дедуп реально
                                          -- держится на UNIQUE(platform, external_review_id) в reviews
    last_seen_review_date TEXT,
    last_checked_at TEXT
);

-- Сами отзывы
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id TEXT NOT NULL REFERENCES locations(id),
    platform TEXT NOT NULL,
    external_review_id TEXT NOT NULL,
    author TEXT,
    rating INTEGER,
    text TEXT,
    review_date TEXT,
    collected_at TEXT NOT NULL,
    sentiment TEXT,                      -- positive | neutral | negative (общий тон отзыва)
    sentiment_score INTEGER,             -- 1-10, интенсивность негатива/уверенность модели
    sentiment_reasoning TEXT,
    urgency INTEGER DEFAULT 0,
    reply_status TEXT DEFAULT 'pending', -- pending | replied | ignored
    reply_draft TEXT,
    reply_sla_deadline TEXT,
    UNIQUE(platform, external_review_id)
);

-- Теги на уровне темы, не на уровне всего отзыва (aspect-based sentiment):
-- позволяет отзыву 4★ содержать "бассейн" с negative-тегом внутри в целом позитивного текста.
CREATE TABLE review_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    tag TEXT NOT NULL,
    tag_sentiment TEXT NOT NULL,         -- positive | neutral | negative
    tag_evidence TEXT,                   -- цитата из отзыва, на основании которой поставлен тег
    zone TEXT                            -- необязательное место (тренажёрный зал/бассейн/...),
                                          -- НЕ участвует в подсчёте порогов Alert Engine (он
                                          -- считает по tag) — только доп.контекст для ручного
                                          -- разбора "жалобы на тему X — в какой зоне больше всего"
);

-- Описания категорий — модель сверяется с ними перед тем, как предложить новый тег
-- или новую категорию, вместо того чтобы плодить дубли под разными формулировками.
CREATE TABLE category_dictionary (
    category TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

-- Управляемый словарь тегов — не даёт разъезжаться формулировкам.
-- description обязателен: именно по нему модель проверяет, не покрывает ли
-- уже существующий тег то, что она хочет предложить как новый.
CREATE TABLE tag_dictionary (
    tag TEXT PRIMARY KEY,
    category TEXT,
    description TEXT,
    status TEXT DEFAULT 'active',        -- active | pending_review
    pending_evidence TEXT,               -- цитата из отзыва, породившая предложение тега
                                          -- (см. PLAN.md "Approval новых тегов") — только
                                          -- для status='pending_review', не нужна после approve
    pending_review_id INTEGER,           -- review_id того же отзыва, для ссылки в Telegram-сообщении
    pending_notified_at TEXT             -- когда отправлено уведомление на approval — не слать дважды
);

-- Персистентное состояние тревоги — не разовое уведомление, а живой статус
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    location_id TEXT NOT NULL REFERENCES locations(id),
    severity TEXT NOT NULL,              -- green | yellow | red
    window_matched INTEGER,
    count_in_window INTEGER,
    first_triggered_at TEXT NOT NULL,
    last_notified_at TEXT,
    status TEXT DEFAULT 'open',          -- open | acknowledged | resolved
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT
);
