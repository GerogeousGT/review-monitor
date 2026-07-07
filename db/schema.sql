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
    last_seen_review_id TEXT,            -- курсор: докуда дособрали при прошлом опросе
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
    reply_detected_at TEXT,
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
    tag_evidence TEXT                    -- цитата из отзыва, на основании которой поставлен тег
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
    status TEXT DEFAULT 'active'         -- active | pending_review
);

-- Настраиваемые пороги алертов (несколько окон на один тег)
CREATE TABLE alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,                   -- '*' = дефолт для всех тегов
    window_days INTEGER NOT NULL,
    yellow_at INTEGER NOT NULL,
    red_at INTEGER NOT NULL
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

-- Тон компании — загруженный документом или добытый из истории ответов
CREATE TABLE tone_profile (
    location_id TEXT PRIMARY KEY REFERENCES locations(id),
    source TEXT NOT NULL,                -- document | mined
    content TEXT,
    updated_at TEXT
);
