"""Золотой стандарт для Sentiment Analyst — прогоняет dataset_<client>.yaml через
реальный LLM-вызов и сравнивает с ручной разметкой. НЕ часть pytest tests/
(платные вызовы, не детерминировано) — запускать вручную при смене
модели/промпта/провайдера, отдельно на каждого клиента:

  .venv/Scripts/python tests/golden/run_eval.py worldclass
  .venv/Scripts/python tests/golden/run_eval.py daudelsport

Каждый клиент — свой файл dataset_<slug>.yaml, свой прогон, без правки общего
кода под конкретного клиента (2026-07-15, решение Жоржа: "надо раздельные
делать, чтоб можно было зайти и запустить тест на конкретного клиента, а не
корректировать эвал каждый раз"). Новый клиент = новый dataset_<slug>.yaml,
этот скрипт не меняется.

Порог принятия — 90% (см. PLAN.md/CHANGELOG 2026-07-15). Ниже — не паника, а сигнал
смотреть руками, что именно разошлось и почему.

Метрика тегов — per-tag (каждый ожидаемый тег засчитывается отдельно), не
per-review "весь отзыв идеально или нет" (2026-07-15: старая all-or-nothing
метрика топила общий процент на многотемных отзывах — 6 из 7 верных тегов
считались полным провалом наравне с 0 из 7, хотя модель на деле понимает
содержание почти полностью, см. PLAN.md/CHANGELOG).
"""
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.sentiment_analyst import analyze_review  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent


def _load_client_dictionaries(client_slug: str) -> tuple[list[dict], list[dict]]:
    """Тег/категория-словарь берётся прямо из client_config.yaml, не из БД —
    датасет должен проверять актуальный конфиг, а не то, что когда-то было
    засеяно в БД (которая могла устареть после ручных правок словаря).

    analyze_review() ожидает category_dictionary в формате БД (поле "category"),
    а не сырого конфига (поле "name") — см. core.db.seed_category_dictionary/
    get_category_dictionary, там эта трансформация происходит неявно через запись
    и чтение из category_dictionary. Здесь конвертируем вручную, без похода в БД."""
    config_path = PROJECT_ROOT / "clients" / client_slug / "client_config.yaml"
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    categories = [{"category": c["name"], "description": c["description"]} for c in cfg["categories"]]
    tags = [{"tag": t["name"], "category": t["category"], "description": t.get("description", "")} for t in cfg["tags"]]
    return tags, categories


def _load_few_shot_examples(client_slug: str) -> list[dict]:
    """Необязательный файл — новый клиент без калибровки его просто не имеет
    (см. core.config.load_few_shot_examples, та же логика без похода через env)."""
    examples_path = PROJECT_ROOT / "clients" / client_slug / "few_shot_examples.yaml"
    if not examples_path.exists():
        return []
    with open(examples_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _compare_tags(expected: list[dict], actual: list[dict], active_tag_names: set[str]) -> dict:
    """Сравнивает ожидаемые теги с фактическими ПО КАЖДОМУ ТЕГУ ОТДЕЛЬНО (recall),
    а не "весь отзыв идеально или нет". Найдено 2026-07-15: на многотемных отзывах
    (7-9 ожидаемых тегов) метрика "всё или ничего на отзыв" топила общий процент —
    отзыв с 6 из 7 верных тегов засчитывался как полный провал наравне с 0 из 7,
    хотя модель на деле понимает содержание почти полностью (см. PLAN.md/CHANGELOG
    2026-07-15 — реальный per-tag recall оказался 76-82%, а не 42-58%, которые
    показывала старая метрика). per-tag остаётся ОСНОВНОЙ метрикой; per-review
    ("идеально ли весь отзыв") — для наглядности, не для решения выше/ниже порога.

    Лишние теги сверх ожидаемых НЕ считаются ошибкой — словарь тегов мог
    справедливо найти больше нюансов, чем разметили вручную (см. dataset_*.yaml).

    Теги, которых НЕТ в активном словаре клиента (active_tag_names), помечаются
    отдельно как "галлюцинация", а не молча засчитываются наравне с реальными —
    см. main_analyze.py: он НЕ доверяет is_new от модели, сверяет с реальным
    словарём в коде. Найдено 2026-07-15: модель иногда возвращает несуществующий
    тег (например "гардероб", "вентиляция" для клиента, где их нет в словаре) с
    is_new=false, то есть ложно заявляет, что тег существующий — эта функция
    должна ловить такое, а не тихо пропускать."""
    actual_by_tag: dict[str, str] = {}
    hallucinated = []
    for a in actual:
        actual_by_tag[a["tag"]] = a["tag_sentiment"]
        if a["tag"] not in active_tag_names and not a.get("is_new"):
            hallucinated.append(a["tag"])

    missing = []
    wrong_sentiment = []
    correct = []
    for exp in expected:
        if exp["tag"] not in actual_by_tag:
            missing.append(exp["tag"])
        elif actual_by_tag[exp["tag"]] != exp["sentiment"]:
            wrong_sentiment.append(f"{exp['tag']} (ожидали {exp['sentiment']}, получили {actual_by_tag[exp['tag']]})")
        else:
            correct.append(exp["tag"])

    review_perfect = not missing and not wrong_sentiment and not hallucinated

    detail_parts = []
    if missing:
        detail_parts.append(f"не найдены теги: {', '.join(missing)}")
    if wrong_sentiment:
        detail_parts.append(f"неверная тональность: {', '.join(wrong_sentiment)}")
    if hallucinated:
        detail_parts.append(f"ГАЛЛЮЦИНАЦИЯ (тег не в словаре, но is_new=false): {', '.join(hallucinated)}")

    return {
        "expected_count": len(expected),
        "found_count": len(correct) + len(wrong_sentiment),  # тег найден, тональность неважна
        "correct_count": len(correct),  # тег найден И тональность верна
        "review_perfect": review_perfect,
        "detail": "; ".join(detail_parts),
    }


def main():
    if len(sys.argv) != 2:
        available = sorted(p.stem.removeprefix("dataset_") for p in GOLDEN_DIR.glob("dataset_*.yaml"))
        print("Использование: run_eval.py <client_slug>")
        print(f"Доступные клиенты: {', '.join(available)}")
        sys.exit(1)

    client_slug = sys.argv[1]
    dataset_path = GOLDEN_DIR / f"dataset_{client_slug}.yaml"
    if not dataset_path.exists():
        print(f"Нет датасета для клиента '{client_slug}': {dataset_path} не найден")
        sys.exit(1)

    with open(dataset_path, encoding="utf-8") as f:
        dataset = yaml.safe_load(f)

    tag_dictionary, category_dictionary = _load_client_dictionaries(client_slug)
    few_shot_examples = _load_few_shot_examples(client_slug)
    active_tag_names = {t["tag"] for t in tag_dictionary}

    total = len(dataset)
    sentiment_correct = 0
    reviews_tags_perfect = 0
    total_expected_tags = 0
    total_tags_found = 0
    total_tags_correct = 0
    failures = []

    for case in dataset:
        try:
            result = analyze_review(case["text"], case["rating"], tag_dictionary, category_dictionary, few_shot_examples)
        except Exception as e:
            failures.append(f"[{case['id']}] ОШИБКА ВЫЗОВА: {e}")
            continue

        sentiment_ok = result["sentiment"] == case["expected_sentiment"]
        if sentiment_ok:
            sentiment_correct += 1
        else:
            failures.append(
                f"[{case['id']}] sentiment: ожидали {case['expected_sentiment']}, получили {result['sentiment']} "
                f"(score={result.get('sentiment_score')}, reasoning={result.get('sentiment_reasoning', '')[:100]})"
            )

        tags_stats = _compare_tags(case["expected_tags"], result.get("aspects", []), active_tag_names)
        total_expected_tags += tags_stats["expected_count"]
        total_tags_found += tags_stats["found_count"]
        total_tags_correct += tags_stats["correct_count"]
        if tags_stats["review_perfect"]:
            reviews_tags_perfect += 1
        elif tags_stats["detail"]:
            failures.append(f"[{case['id']}] tags: {tags_stats['detail']}")

    tag_recall = total_tags_found / total_expected_tags if total_expected_tags else 1.0
    tag_precision_sentiment = total_tags_correct / total_expected_tags if total_expected_tags else 1.0

    print(f"\n{'='*60}")
    print(f"Клиент: {client_slug}")
    print(f"Всего кейсов: {total}, ожидаемых тегов всего: {total_expected_tags}")
    print(f"Sentiment верно:            {sentiment_correct}/{total} ({100*sentiment_correct/total:.0f}%)")
    print(f"--- Основная метрика: per-tag (каждый тег отдельно, не весь отзыв целиком) ---")
    print(f"Тег найден (тональность неважна): {total_tags_found}/{total_expected_tags} ({100*tag_recall:.0f}%)")
    print(f"Тег найден И тональность верна:   {total_tags_correct}/{total_expected_tags} ({100*tag_precision_sentiment:.0f}%)")
    print(f"--- Справочно: per-review (весь отзыв идеально, старая метрика — не для решения по порогу) ---")
    print(f"Отзывов разобрано идеально: {reviews_tags_perfect}/{total} ({100*reviews_tags_perfect/total:.0f}%)")
    print(f"{'='*60}")

    if failures:
        print("\nРасхождения:")
        for f in failures:
            print(f"  - {f}")

    overall = (sentiment_correct / total + tag_precision_sentiment) / 2
    print(f"\nОбщий accuracy (sentiment + per-tag): {100*overall:.0f}% (порог принятия — 90%)")
    if overall < 0.9:
        print("⚠️  Ниже порога — посмотреть расходящиеся кейсы руками, не паниковать, но разобраться.")
    else:
        print("✅  Выше порога.")


if __name__ == "__main__":
    main()
