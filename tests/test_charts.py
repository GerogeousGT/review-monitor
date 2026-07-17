"""webapp/charts.py — render_diverging_bar_chart. webapp/ не пакет (нет
__init__.py, живёт в своём отдельном venv на VPS — см. README "Установка"), но
charts.py — чистый stdlib-модуль (только html), поэтому тестируем его из
корневого venv через прямую вставку webapp/ в sys.path, как это уже делает сам
webapp/app.py в обратную сторону (вставляет PROJECT_ROOT, чтобы достать core/agents)."""
import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
sys.path.insert(0, str(WEBAPP_DIR))

from charts import (  # noqa: E402
    render_diverging_bar_chart,
    _rounded_top_path,
    _rounded_bottom_path,
    _esc,
    _nice_ceiling,
    _plural_reviews,
)


def _period(label, positive=0, neutral=0, negative=0):
    return {"period": label, "label": label, "positive": positive, "neutral": neutral, "negative": negative}


def test_empty_periods_returns_valid_svg_without_error():
    svg = render_diverging_bar_chart([])
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "Нет данных" in svg


def test_renders_valid_svg_wrapper():
    svg = render_diverging_bar_chart([_period("15 июл", positive=3, neutral=1, negative=2)])
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert 'role="img"' in svg


def test_all_zero_period_does_not_crash():
    """Период без единого отзыва (см. core.db zero-fill) — max_val должен упасть
    на пол 1, не на ZeroDivisionError."""
    svg = render_diverging_bar_chart([_period("14 июл"), _period("15 июл")])
    assert svg.startswith("<svg")


def test_positive_and_negative_segments_present_when_nonzero():
    svg = render_diverging_bar_chart([_period("15 июл", positive=5, neutral=0, negative=3)])
    # positive/negative — path-сегменты (закруглённые), neutral=0 — rect для нейтрали отсутствует
    assert svg.count("<path") == 2  # один вверх (positive), один вниз (negative)
    assert f'fill="{"#5eeaa8"}"' in svg  # positive color
    assert f'fill="{"#ea5e6e"}"' in svg  # negative color


def test_neutral_only_period_renders_single_rect_no_paths():
    svg = render_diverging_bar_chart([_period("15 июл", positive=0, neutral=4, negative=0)])
    assert "#e0b84c" in svg  # neutral color
    assert svg.count("<path") == 0  # ни positive, ни negative сегмента


def test_label_escaping_prevents_markup_injection():
    """Периодные подписи сейчас всегда server-computed (core.db._period_bucket),
    но экранирование — defense-in-depth: если сюда когда-нибудь попадёт
    непредсказуемый текст, кавычка не должна выйти НЕЭКРАНИРОВАННОЙ — иначе она
    разорвала бы атрибут и позволила бы вставить произвольный HTML-атрибут вроде
    onload=. Сам текст "onload=" безопасно остаётся как экранированный контент —
    проверяем не его отсутствие, а то, что кавычка вокруг него обезврежена."""
    malicious = _period('15" onload="alert(1)', positive=1)
    svg = render_diverging_bar_chart([malicious])
    assert '15" onload="alert(1)' not in svg  # неэкранированная кавычка не прошла как есть
    assert "&quot;" in svg  # экранированная версия присутствует


def test_custom_colors_override_defaults():
    svg = render_diverging_bar_chart(
        [_period("15 июл", positive=2, negative=1)],
        colors={"positive": "#111111", "neutral": "#222222", "negative": "#333333"},
    )
    assert "#111111" in svg
    assert "#333333" in svg


def test_many_periods_thins_labels_by_max_labels():
    periods = [_period(f"день {i}", positive=1) for i in range(30)]
    svg = render_diverging_bar_chart(periods, max_labels=10)
    # <text> — только подписи баров (не hit-area/path/rect), должно быть заметно
    # меньше 30 при прореживании, но первая и последняя подписи всегда есть
    label_count = svg.count("<text")
    assert label_count < 30
    assert "день 0" in svg
    assert "день 29" in svg  # последний период не пропущен


def test_rounded_top_path_zero_height_returns_empty():
    assert _rounded_top_path(0, 0, 10, 0, 4) == ""


def test_rounded_bottom_path_zero_width_returns_empty():
    assert _rounded_bottom_path(0, 0, 0, 10, 4) == ""


def test_rounded_path_radius_clamped_to_small_segment():
    """Радиус 4px на сегменте высотой 2px не должен ломать геометрию (r=min(r,h/2))."""
    path = _rounded_top_path(0, 0, 20, 2, 4)
    assert path  # не пусто, не упало
    assert "Q" in path  # закругление всё ещё есть (r=1), просто меньше


def test_esc_escapes_html_special_chars():
    assert _esc('<script>alert("x")</script>') == "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"


def test_nice_ceiling_rounds_up_to_clean_numbers():
    assert _nice_ceiling(4) == 5
    assert _nice_ceiling(5) == 5  # уже круглое — не завышаем зря
    assert _nice_ceiling(23) == 50
    assert _nice_ceiling(1) == 1
    assert _nice_ceiling(0) == 1  # защита от log10(0)
    assert _nice_ceiling(100) == 100  # уже круглое (лестница 1/2/5/10×10^n не включает 150)
    assert _nice_ceiling(101) == 200


def test_plural_reviews_forms():
    assert _plural_reviews(1) == "отзыв"
    assert _plural_reviews(2) == "отзыва"
    assert _plural_reviews(4) == "отзыва"
    assert _plural_reviews(5) == "отзывов"
    assert _plural_reviews(11) == "отзывов"  # исключение "-надцать"
    assert _plural_reviews(21) == "отзыв"
    assert _plural_reviews(0) == "отзывов"


def test_axis_shows_shared_scale_both_sides():
    """Один и тот же масштаб сверху и снизу — иначе бар '5' позитива и бар '5'
    негатива выглядели бы разной высоты (визуальная ложь, см. docstring модуля)."""
    svg = render_diverging_bar_chart([_period("15 июл", positive=23, negative=3)])
    assert svg.count(">50<") == 2  # _nice_ceiling(23)=50, показано И сверху, И снизу
    assert ">0<" in svg  # подпись нулевой линии


def test_hit_rect_has_tooltip_data_with_total_and_breakdown():
    svg = render_diverging_bar_chart([_period("15 июл", positive=2, neutral=1, negative=1)])
    assert 'class="chart-bar-hit"' in svg
    assert 'data-tooltip="15 июл — 4 отзыва: 2' in svg
    assert 'tabindex="0"' in svg
    assert "aria-label=" in svg


def test_no_native_title_element():
    """Тултип теперь через data-tooltip + JS в шаблоне, не нативный SVG <title>
    (медленный, нестилизованный системный тултип, см. docstring модуля)."""
    svg = render_diverging_bar_chart([_period("15 июл", positive=1)])
    assert "<title>" not in svg


def test_visible_segments_have_pointer_events_none():
    """Видимые сегменты не должны перехватывать hover у прозрачного hit-rect
    под ними — иначе наведение на видимую часть бара не поймает tooltip."""
    svg = render_diverging_bar_chart([_period("15 июл", positive=2, neutral=1, negative=1)])
    assert 'pointer-events="none"' in svg
