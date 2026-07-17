"""SVG-рендер графиков дашборда — inline, без внешних JS-библиотек (весь webapp
server-rendered Flask+Jinja2, см. README "Установка" — ни одной внешней
JS-зависимости в проекте нет, CDN-скрипт добавлял бы точку отказа и CSP-риск
ради одного статичного графика без анимаций/зума).

render_diverging_bar_chart — колонки, расходящиеся от нулевой линии (позитив
вверх, негатив вниз, нейтраль — тонкая полоска поперёк нуля). Форма выбрана по
методичке dataviz-skill (choosing-a-form.md): тональность — "ordered-scale
share (Likert, sentiment, agree↔disagree)" → diverging stacked bar, centered on
neutral, не line chart. Цвета — НЕ дефолтная палитра skill'а, а те же
green/yellow/red, что уже используются в остальном продукте для тональности
(статус-бар дашборда, иконки в Telegram-карточках) — сознательно, чтобы не
завести второй визуальный язык для одного и того же смысла.

Ось Y (2026-07-17, запрос Жоржа) — один и тот же масштаб для позитивной и
негативной стороны (не отдельные шкалы вверх/вниз — иначе бар "5" сверху и бар
"5" снизу выглядели бы разной высоты, визуальная ложь). Подпись — "круглое"
число (1/2/5×10ⁿ, mark spec "round to clean numbers"), не сырой максимум.

Тултип при наведении (2026-07-17) — НЕ нативный SVG <title> (медленный,
нестилизованный системный тултип), а data-атрибут (`data-tooltip`), который
подхватывает JS в dashboard.html (тот же паттерн vanilla JS, что уже есть в
файле для модалки drill-down по алерту — не новый фреймворк). aria-label на
hit-rect остаётся для доступности (screen reader получает текст без наведения).

Низкоуровневые SVG-хелперы (escape, rounded-rect path, nice-ceiling) вынесены
отдельно, чтобы будущие графики того же рода задач (например тренд числа
упоминаний тега во времени) их переиспользовали — НО это будет ДРУГАЯ форма
(одна серия, без divergence — "trend over time" по той же методичке —
line/single-hue bar), не эта функция буквально; общая только инфраструктура
агрегации по периодам (core.db.get_review_sentiment_counts_by_period — сам
паттерн bucketing) и низкоуровневые SVG-примитивы ниже.

Безопасность: проект принципиально не использует |safe/innerHTML нигде в
шаблонах (Jinja2 autoescape). Эта функция — первое место, где сырой HTML
(SVG-разметка) помечается доверенным в шаблоне (`{{ ... | safe }}`, ровно одно
место в dashboard.html), поэтому все текстовые значения, которые теоретически
МОГЛИ БЫ быть непредсказуемыми, экранируются здесь же через _esc(), а не
полагаются на шаблон. По факту единственный текст в графике — периодные
подписи ("15 июл", "нед. 29"), целиком вычисленные сервером из дат
(core.db._period_bucket), не текст отзыва/автора/тега — но экранирование
оставлено как defense-in-depth, а не потому что конкретно сейчас есть реальный
вектор атаки. Возвращает обычный str, не markupsafe.Markup — Flask/Jinja2 не
входят в корневой requirements.txt (webapp/ живёт в своём venv, см. README
"Установка"), а этот модуль должен оставаться тестируемым из корневого venv
без новых зависимостей (та же причина, по которой agents/notifier.py вручную
делает html.escape вместо стороннего HTML-билдера)."""
import html
import math

# Те же цвета, что и в остальном дашборде/Telegram для тональности (см. docstring
# выше) — base.html: --accent (зелёный), dashboard.html: #e0b84c (жёлтый,
# .sentiment-bar .seg.neutral), --danger (красный).
SENTIMENT_COLORS = {"positive": "#5eeaa8", "neutral": "#e0b84c", "negative": "#ea5e6e"}

_GAP = 2  # surface gap между соприкасающимися сегментами стека (mark spec)
_RADIUS = 4  # радиус закругления внешнего края сегмента (mark spec: "4px rounded data-end")
_AXIS_MARGIN_LEFT = 30  # место под подписи оси Y слева от графика


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _plural_reviews(n: int) -> str:
    """Русское склонение "отзыв/отзыва/отзывов" по числу n — для тултипа
    ("15 июл — 4 отзыва: ..."), запрос Жоржа 2026-07-17."""
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return "отзыв"
    if 2 <= n_abs % 10 <= 4 and not (12 <= n_abs % 100 <= 14):
        return "отзыва"
    return "отзывов"


def _nice_ceiling(value: float) -> int:
    """Наименьшее "круглое" число (1/2/5 × 10^n) не меньше value — подпись оси
    должна быть читаемой, не сырым максимумом данных (mark spec "round to
    clean numbers")."""
    if value <= 0:
        return 1
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 2, 5, 10):
        candidate = mult * base
        if candidate >= value - 1e-9:
            return int(round(candidate))
    return int(round(10 * base))  # недостижимо (mult=10 всегда покрывает), защитный fallback


def _rounded_top_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """Прямоугольник с закруглёнными ВЕРХНИМИ углами, острыми нижними —
    "4px rounded data-end, square at the baseline" (dataviz skill,
    marks-and-anatomy.md). Для сегмента, растущего ВВЕРХ от нулевой линии —
    внешний (верхний) край закруглён, край у базовой линии (нижний) острый."""
    if h <= 0 or w <= 0:
        return ""
    r = min(r, h / 2, w / 2)
    if r <= 0:
        return f"M{x},{y} h{w} v{h} h{-w} Z"
    return (
        f"M{x},{y + h} V{y + r} Q{x},{y} {x + r},{y} "
        f"H{x + w - r} Q{x + w},{y} {x + w},{y + r} V{y + h} Z"
    )


def _rounded_bottom_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """Зеркало _rounded_top_path — для сегмента, растущего ВНИЗ от нулевой линии
    (закруглён нижний/внешний край, острый верхний/у базовой линии)."""
    if h <= 0 or w <= 0:
        return ""
    r = min(r, h / 2, w / 2)
    if r <= 0:
        return f"M{x},{y} h{w} v{h} h{-w} Z"
    return (
        f"M{x},{y} H{x + w} V{y + h - r} Q{x + w},{y + h} {x + w - r},{y + h} "
        f"H{x + r} Q{x},{y + h} {x},{y + h - r} V{y} Z"
    )


def render_diverging_bar_chart(
    periods: list[dict],
    colors: dict | None = None,
    width: int = 960,
    height: int = 200,
    max_labels: int = 15,
) -> str:
    """periods — прямой выход core.db.get_review_sentiment_counts_by_period:
    [{"period": "2026-07-15", "label": "15 июл", "positive": N, "neutral": N, "negative": N}, ...]

    Каждая колонка расходится от нулевой линии: позитив вверх, негатив вниз,
    нейтраль — полоска поперёк нуля (наполовину выше, наполовину ниже baseline).
    Слева — ось Y: "0" на базовой линии, круглое число сверху/снизу (общий
    масштаб на обе стороны). Hover — прозрачный hit-rect на всю высоту колонки
    с `data-tooltip` (подхватывается JS в dashboard.html) и `aria-label` для
    доступности.

    max_labels — если периодов больше, подписи прореживаются (mark spec:
    измерить, не клипать текст — прежде чем рисовать каждую подпись, решаем,
    поместятся ли все без наложения)."""
    if colors is None:
        colors = SENTIMENT_COLORS

    n = len(periods)
    if n == 0:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="Нет данных за период"></svg>'
        )

    label_area = 22
    plot_top = 6
    plot_bottom = height - label_area
    plot_height = plot_bottom - plot_top
    baseline_y = plot_top + plot_height / 2
    half = plot_height / 2

    raw_max_pos = max((p["positive"] + p["neutral"] / 2) for p in periods)
    raw_max_neg = max((p["negative"] + p["neutral"] / 2) for p in periods)
    # Общий "круглый" масштаб на обе стороны — см. docstring модуля: разные
    # шкалы вверх/вниз для одной и той же величины были бы визуальной ложью.
    axis_max = _nice_ceiling(max(raw_max_pos, raw_max_neg, 1))
    scale = (half - 2) / axis_max  # -2px запас, чтобы пик не упирался в самый край

    plot_area_width = width - _AXIS_MARGIN_LEFT
    bar_slot = plot_area_width / n
    bar_width = max(min(24, bar_slot - _GAP), 1)
    label_step = max(1, round(n / max_labels))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Динамика тональности по периодам, шкала до {axis_max} отзывов">',
        # Ось Y: 0 на базовой линии, axis_max сверху и снизу (единый масштаб)
        f'<text x="{_AXIS_MARGIN_LEFT - 6}" y="{baseline_y + 3:.1f}" text-anchor="end" '
        f'font-size="10" fill="var(--text-dim)">0</text>',
        f'<text x="{_AXIS_MARGIN_LEFT - 6}" y="{plot_top + 8}" text-anchor="end" '
        f'font-size="10" fill="var(--text-dim)">{axis_max}</text>',
        f'<text x="{_AXIS_MARGIN_LEFT - 6}" y="{plot_bottom:.1f}" text-anchor="end" '
        f'font-size="10" fill="var(--text-dim)">{axis_max}</text>',
        f'<line x1="{_AXIS_MARGIN_LEFT}" y1="{baseline_y:.1f}" x2="{width}" y2="{baseline_y:.1f}" '
        f'stroke="var(--border)" stroke-width="1"/>',
    ]

    for i, p in enumerate(periods):
        x = _AXIS_MARGIN_LEFT + i * bar_slot + (bar_slot - bar_width) / 2
        pos, neu, neg = p["positive"], p["neutral"], p["negative"]
        total = pos + neu + neg

        neutral_half_h = (neu / 2) * scale
        pos_h = pos * scale
        neg_h = neg * scale
        neutral_top_edge = baseline_y - neutral_half_h
        neutral_bottom_edge = baseline_y + neutral_half_h

        tooltip = (
            f"{p['label']} — {total} {_plural_reviews(total)}: "
            f"{pos} \U0001F7E2 · {neu} \U0001F7E1 · {neg} \U0001F534"
        )

        parts.append("<g>")
        # Прозрачный hit-area на всю высоту колонки — hover/focus срабатывает
        # даже когда видимые сегменты малы или отсутствуют (пустой период).
        parts.append(
            f'<rect class="chart-bar-hit" x="{x:.2f}" y="{plot_top}" width="{bar_width:.2f}" '
            f'height="{plot_height}" fill="transparent" data-tooltip="{_esc(tooltip)}" '
            f'aria-label="{_esc(tooltip)}" tabindex="0"/>'
        )

        if neu > 0:
            parts.append(
                f'<rect x="{x:.2f}" y="{neutral_top_edge:.2f}" width="{bar_width:.2f}" '
                f'height="{neutral_half_h * 2:.2f}" rx="{min(_RADIUS, neutral_half_h):.2f}" '
                f'fill="{colors["neutral"]}" pointer-events="none"/>'
            )

        if pos_h > 0:
            seg_gap = _GAP if neu > 0 else 0
            seg_h = max(pos_h - seg_gap, 1)
            seg_bottom = neutral_top_edge - seg_gap
            path = _rounded_top_path(x, seg_bottom - seg_h, bar_width, seg_h, _RADIUS)
            if path:
                parts.append(f'<path d="{path}" fill="{colors["positive"]}" pointer-events="none"/>')

        if neg_h > 0:
            seg_gap = _GAP if neu > 0 else 0
            seg_h = max(neg_h - seg_gap, 1)
            seg_top = neutral_bottom_edge + seg_gap
            path = _rounded_bottom_path(x, seg_top, bar_width, seg_h, _RADIUS)
            if path:
                parts.append(f'<path d="{path}" fill="{colors["negative"]}" pointer-events="none"/>')

        parts.append("</g>")

        if i % label_step == 0 or i == n - 1:
            label_x = x + bar_width / 2
            parts.append(
                f'<text x="{label_x:.2f}" y="{height - 6}" text-anchor="middle" '
                f'font-size="10" fill="var(--text-dim)">{_esc(p["label"])}</text>'
            )

    parts.append("</svg>")
    return "".join(parts)
