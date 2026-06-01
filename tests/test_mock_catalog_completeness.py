from app.mock.meituan import MeituanMock


SCENES = ("family", "couple", "friends", "solo")
SEGMENTS = ("play", "eat", "extra")


def test_mock_catalog_has_enough_depth_for_every_scene_segment():
    mock = MeituanMock()

    assert len(mock._items) >= 30

    for segment in SEGMENTS:
        for scene in SCENES:
            candidates = mock.search_merchants(segment=segment, scene=scene, limit=10)

            assert len(candidates) >= 3, (segment, scene, [c["id"] for c in candidates])


def test_dianping_search_uses_query_area_and_preferences_for_ranking():
    mock = MeituanMock()

    results = mock.search_merchants(
        segment="eat",
        scene="solo",
        query="一个人在新街口附近，想吃清淡一点，安静坐一会儿",
        limit=5,
    )

    assert results
    assert results[0]["id"] in {"deji_green_tea", "xjk_lotus_light", "deji_thai_bistro"}
    assert "新街口" in results[0]["match_reasons"]
    assert any(reason in results[0]["match_reasons"] for reason in ("清淡", "安静", "一个人"))


def test_dianping_detail_returns_full_mock_detail_surface():
    mock = MeituanMock()

    detail = mock.detail("deji_green_tea")

    assert detail["id"] == "deji_green_tea"
    assert len(detail["gallery"]) >= 2
    assert len(detail["dianping_notes"]) >= 2
    assert len(detail["reviews"]) >= 3
    assert detail["groupon"]
    assert detail["queue"]["has_table"] is True
    assert detail["booking"]["type"] == "table"


def test_meituan_query_queue_returns_actionable_availability():
    mock = MeituanMock()

    blocked = mock.query_queue("green_bowl_hexi", party_size=3)
    open_table = mock.query_queue("deji_green_tea", party_size=1)

    assert blocked["status"] == "no_table"
    assert blocked["has_table"] is False
    assert blocked["recommended_action"] == "switch_candidate"
    assert open_table["status"] == "available"
    assert open_table["has_table"] is True
    assert open_table["recommended_action"] == "book_table"
