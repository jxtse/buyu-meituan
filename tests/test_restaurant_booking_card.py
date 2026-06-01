from app.session import Recommendation


def _rec(segment_kind: str, booking: dict) -> Recommendation:
    return Recommendation(
        segment_kind=segment_kind,
        segment_intent="",
        poi={
            "id": "poi_1",
            "name": "测试餐厅",
            "booking": booking,
            "queue": {"need_queue": True, "wait_min": 18, "has_table": True},
            "groupon": [],
        },
        summary="",
        groupon_id=None,
        groupon_reason="",
        suggestion="",
        confidence=0.9,
    )


def test_eat_card_includes_pending_number_for_two_person_table():
    card = _rec("eat", {"type": "table", "label": "预约双人桌"}).to_card()

    assert card["booking_execution"] == {
        "status": "pending",
        "title": "预备取号 2 人位",
        "subtitle": "确认方案后执行取号，到店前可按号候位",
        "wait_min": 18,
    }


def test_non_restaurant_card_omits_booking_execution():
    card = _rec("play", {"type": "ticket", "label": "预约亲子票"}).to_card()

    assert "booking_execution" not in card
