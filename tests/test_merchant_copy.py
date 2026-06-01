import json
from pathlib import Path


def _merchants() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "app/mock/data/merchants.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_chinese_place_names_do_not_mix_city_walk_suffix():
    names = [m["name"] for m in _merchants()]

    assert "夫子庙 · 老南京小吃 City Walk" not in names
    assert all(not name.endswith(" City Walk") for name in names)


def test_display_categories_are_short_for_card_chips():
    categories = {m["category"] for m in _merchants()}

    assert "City Walk/街区" not in categories
    assert all(len(category) <= 6 for category in categories)
