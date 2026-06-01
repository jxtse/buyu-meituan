from app.session import (
    _normalize_intent,
    _normalize_recommendation,
    _normalize_segment_plan,
)


def test_normalize_claude_nested_family_intent_schema():
    raw = {
        "constraints": {
            "同行人员": {
                "总人数": 3,
                "儿童": {"有无": True, "年龄": 5},
            },
            "活动需求": {"核心活动": "遛娃/亲子游玩"},
            "餐饮需求": {"档次": "好一点"},
            "地点约束": {"距离要求": "近（别离家太远）"},
            "时间约束": {"时段": "周末下午"},
        },
        "preferences": {"整体调性": ["轻松", "家庭", "便利"]},
    }

    normalized = _normalize_intent(raw, "周末下午带老婆孩子出来玩，孩子5岁，别离家太远")

    assert normalized["scene"] == "family"
    assert normalized["adults"] == 2
    assert normalized["kids"] == 1
    assert normalized["kid_age"] == 5
    assert normalized["time_window"] == {"start": "14:00", "hours": 4}
    assert "就近" in normalized["preferences"]


def test_normalize_claude_english_nested_intent_schema():
    raw = {
        "解析结果": {
            "constraints": {
                "companions": {"adults": 2, "children": 1, "child_age": 5},
                "location": {"distance_preference": "就近，别离家太远"},
                "activity_requirements": {"primary": "遛娃/亲子活动"},
                "dining_requirements": {"quality": "吃顿好的（品质优先）"},
            },
            "preferences": {"convenience": "玩乐与餐饮最好在同一区域"},
        }
    }

    normalized = _normalize_intent(raw, "周末下午带老婆孩子出来玩，孩子5岁，别离家太远")

    assert normalized["scene"] == "family"
    assert normalized["adults"] == 2
    assert normalized["kids"] == 1
    assert normalized["kid_age"] == 5
    assert normalized["budget_level"] == "high"
    assert "亲子" in normalized["preferences"]


def test_normalize_intent_corrects_strong_solo_query_signals():
    raw = {
        "scene": "friends",
        "adults": 2,
        "kids": 0,
        "kid_age": None,
        "diet": [],
        "time_window": {"start": "14:00", "hours": 4},
        "budget_level": "medium",
        "preferences": ["慢节奏"],
        "summary": "从当前位置出发，安排适合friends场景的活动和餐食。",
    }

    normalized = _normalize_intent(
        raw,
        "今晚想一个人在新街口附近吃点清淡的，再找个安静地方坐一会",
    )

    assert normalized["scene"] == "solo"
    assert normalized["adults"] == 1
    assert normalized["kids"] == 0
    assert {"一个人", "新街口", "清淡", "安静"}.issubset(set(normalized["preferences"]))
    assert "friends" not in normalized["summary"]
    assert "朋友" not in normalized["summary"]


def test_normalize_claude_segment_plan_schema():
    raw = {
        "itinerary": [
            {"type": "亲子游玩", "goal": "先安排室内遛娃"},
            {"type": "晚餐", "goal": "再找适合家庭的餐厅"},
            {"type": "消食散步", "goal": "饭后轻松收尾"},
        ],
        "opening": "我会按先玩再吃再收尾来安排。",
    }

    normalized = _normalize_segment_plan(raw)

    assert normalized == {
        "segments": [
            {"kind": "play", "intent": "先安排室内遛娃"},
            {"kind": "eat", "intent": "再找适合家庭的餐厅"},
            {"kind": "extra", "intent": "饭后轻松收尾"},
        ],
        "narrative": "我会按先玩再吃再收尾来安排。",
    }


def test_normalize_claude_recommendation_schema_maps_name_to_candidate_id():
    raw = {
        "推荐地点": "河西 Kiddo Lab 亲子科学实验场",
        "推荐理由": "离得近，5岁孩子能做小实验，家长也轻松。",
        "推荐套餐": "周末亲子探索 2大1小套票",
        "补充建议": "建议先玩90分钟，再去附近吃饭。",
        "confidence_score": 0.87,
    }
    candidates = [
        {
            "id": "kiddo_lab",
            "name": "河西 Kiddo Lab 亲子科学实验场",
            "ai_pitch": "",
            "groupon": [{"id": "grp_kiddo_family", "title": "周末亲子探索 2大1小套票"}],
        }
    ]

    normalized = _normalize_recommendation(raw, candidates)

    assert normalized["chosen_id"] == "kiddo_lab"
    assert normalized["groupon_id"] == "grp_kiddo_family"
    assert normalized["summary"] == "离得近，5岁孩子能做小实验，家长也轻松。"
    assert normalized["suggestion"] == "建议先玩90分钟，再去附近吃饭。"
    assert normalized["confidence"] == 0.87
