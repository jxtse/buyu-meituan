from app.mock.meituan import MeituanMock


def _merchants() -> list[dict]:
    return MeituanMock()._items


def test_chinese_place_names_do_not_mix_city_walk_suffix():
    names = [m["name"] for m in _merchants()]

    assert "夫子庙 · 老南京小吃 City Walk" not in names
    assert all(not name.endswith(" City Walk") for name in names)


def test_display_categories_are_short_for_card_chips():
    categories = {m["category"] for m in _merchants()}

    assert "City Walk/街区" not in categories
    assert all(len(category) <= 6 for category in categories)
