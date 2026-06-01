from app.mock.meituan import MeituanMock
from app.session import Session


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def chat_json(self, *, system, user, model=None, retries=0):
        self.calls += 1
        return {
            "scene": "family",
            "adults": 2,
            "kids": 1,
            "kid_age": 5,
            "diet": [],
            "time_window": {"start": "14:00", "hours": 4},
            "budget_level": "medium",
            "preferences": ["亲子"],
            "summary": "自定义 LLM 解析结果",
        }, None


class CollectingBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _session():
    bus = CollectingBus()
    llm = FakeLLM()
    return Session(llm=llm, mock=MeituanMock(bus=bus), bus=bus), llm, bus


def test_preset_start_does_not_call_llm_and_returns_first_card():
    session, llm, bus = _session()

    result = session.start(
        "周末下午带老婆孩子出来玩，孩子5岁，想找个能遛娃的地方，再吃顿好的，别离家太远",
        source="preset",
        preset_id="family",
    )

    assert llm.calls == 0
    assert result["constraints"]["scene"] == "family"
    assert result["card"]["poi"]["id"] == "kiddo_lab"
    assert [s["kind"] for s in result["segments"]] == ["play", "eat", "extra"]
    assert result["agent_delay_ms"] > 0
    assert len(result["agent_events"]) >= 8
    assert result["agent_events"][0]["type"] == "stage"
    assert any(e.get("type") == "tool_call" and e.get("name") == "meituan_locate"
               for e in result["agent_events"])
    assert any(e.get("type") == "card" and e.get("card", {}).get("poi", {}).get("id") == "kiddo_lab"
               for e in result["agent_events"])
    assert any(e.get("type") == "thinking" and "预设场景" in e.get("text", "") for e in bus.events)
    assert any(e.get("type") == "tool_call" and e.get("name") == "dianping_search" for e in bus.events)


def test_custom_start_still_calls_llm_for_intent():
    session, llm, _bus = _session()

    result = session.start("自定义：下午想找个安静的地方吃饭", source="custom")

    assert llm.calls == 1
    assert result["constraints"]["summary"] == "自定义 LLM 解析结果"


def test_custom_start_greeting_asks_for_needs_without_recommending_card():
    session, llm, bus = _session()

    result = session.start("hi", source="custom")

    assert llm.calls == 0
    assert result["reply"]
    assert "小团" in result["reply"]
    assert "同行" in result["reply"]
    assert result["needs_more_info"] is True
    assert result["card"] is None
    assert result["segments"] == []
    assert result["intake_active"] is True
    assert result["intake_options"]
    assert {g["slot"] for g in result["intake_options"]} >= {"goal", "companions", "time", "area"}
    assert any(o["value"] == "散步" for g in result["intake_options"] for o in g["options"])
    assert any(o["value"] == "哪里都行" for g in result["intake_options"] for o in g["options"])
    assert any(e.get("type") == "tool_call" and e.get("name") == "agent_speak"
               for e in result["agent_events"])
    assert any(e.get("type") == "assistant_reply" and e.get("text") == result["reply"]
               for e in bus.events)


def test_intake_agent_remembers_context_and_asks_specific_followup():
    session, llm, bus = _session()
    first = session.start("Hello", source="custom")

    result = session.chat("找个地方坐一会儿")

    assert first["intake_active"] is True
    assert llm.calls == 0
    assert result["needs_more_info"] is True
    assert result["card"] is None
    assert result["reply"] != first["reply"]
    assert "你好，我是小团" not in result["reply"]
    assert "附近" in result["reply"]
    assert "一个人" in result["reply"]
    assert {g["slot"] for g in result["intake_options"]} == {"area", "companions", "time"}
    assert any(o["value"] == "和朋友" for g in result["intake_options"] for o in g["options"])
    assert any(o["value"] == "现在去" for g in result["intake_options"] for o in g["options"])
    assert "坐一会儿" in session.intake_memory["preferences"]
    assert any(e.get("type") == "tool_call" and e.get("name") == "agent_memory_write"
               for e in bus.events)
    assert any(e.get("type") == "thinking" and "ReAct" in e.get("text", "")
               for e in bus.events)


def test_intake_agent_completes_to_tool_backed_card_when_enough_details_arrive():
    session, llm, bus = _session()
    session.start("hi", source="custom")
    session.chat("找个地方坐一会儿")

    result = session.chat("一个人，在新街口附近，下午，想安静点，吃点清淡的")

    assert llm.calls == 1
    assert result.get("needs_more_info") is not True
    assert result["card"]
    assert result["segments"]
    assert result["constraints"]["scene"] == "solo"
    assert result["constraints"]["adults"] == 1
    assert "安静" in result["constraints"]["preferences"]
    assert "清淡" in result["constraints"]["preferences"]
    assert result["agent_events"]
    assert any(e.get("type") == "tool_call" and e.get("name") == "dianping_search"
               for e in bus.events)
    assert any(e.get("type") == "tool_call" and e.get("name") == "dianping_detail"
               for e in bus.events)
    assert any(e.get("type") == "tool_call" and e.get("name") == "show_recommendation_card"
               for e in bus.events)


def test_intake_agent_treats_now_and_anywhere_as_enough_context():
    session, llm, bus = _session()
    session.start("hi", source="custom")
    session.chat("散步")
    second = session.chat("和朋友")

    result = session.chat("现在去，哪里都行")

    assert second["needs_more_info"] is True
    assert llm.calls == 1
    assert result.get("needs_more_info") is not True
    assert result["card"]
    assert result["segments"]
    assert session.intake_memory["time"] == "现在"
    assert session.intake_memory["area"] == "不限区域"
    assert "哪里都行" in session.intake_memory["preferences"]
    assert "现在" in result["constraints"]["preferences"]
    assert "哪里都行" in result["constraints"]["preferences"]
    assert any(e.get("type") == "tool_call" and e.get("name") == "dianping_search"
               for e in bus.events)


def test_all_home_presets_return_expected_scene_and_card():
    expected = {
        "family": "family",
        "couple": "couple",
        "friends": "friends",
        "solo": "solo",
    }

    for preset_id, scene in expected.items():
        session, llm, _bus = _session()
        result = session.start("preset query", source="preset", preset_id=preset_id)

        assert llm.calls == 0
        assert result["constraints"]["scene"] == scene
        assert result["card"]["poi"]["segment"] == "play"
        assert result["agent_events"]


def test_solo_preset_uses_solo_compatible_candidate_not_family_fallback():
    session, llm, _bus = _session()

    result = session.start("一个人想出去走走放松下", source="preset", preset_id="solo")
    search_result = next(
        e for e in result["agent_events"]
        if e.get("type") == "tool_result" and e.get("name") == "dianping_search"
    )

    assert llm.calls == 0
    assert result["constraints"]["scene"] == "solo"
    assert result["card"]["poi"]["id"] != "kiddo_lab"
    assert result["card"]["poi"]["segment"] == "play"
    assert "solo" in result["card"]["poi"]["scenes"]
    assert not result["card"].get("groupon")
    assert search_result["result"]["count"] > 0


def test_all_home_presets_return_complete_card_speech():
    for preset_id in ("family", "couple", "friends", "solo"):
        session, _llm, _bus = _session()

        card = session.start("preset query", source="preset", preset_id=preset_id)["card"]

        assert card["summary"]
        assert card["suggestion"]


def test_accept_returns_synchronized_agent_events_for_next_card():
    session, _llm, _bus = _session()
    session.start("preset query", source="preset", preset_id="family")

    result = session.accept()

    assert result["card"]["poi"]["segment"] == "eat"
    assert result["card"]["poi"]["id"] == "sprout_table"
    assert result["agent_delay_ms"] > 0
    assert result["agent_events"][0]["type"] == "user_action"
    assert "就这家" in result["agent_events"][0]["text"]
    assert any(e.get("type") == "tool_call" and e.get("name") == "dianping_search"
               for e in result["agent_events"])
    assert any(e.get("type") == "tool_call" and e.get("name") == "meituan_query_queue"
               for e in result["agent_events"])
    assert any(e.get("type") == "tool_result" and e.get("name") == "meituan_query_queue"
               and e.get("result", {}).get("status") == "no_table"
               for e in result["agent_events"])
    thought_texts = [e.get("text", "") for e in result["agent_events"]
                     if e.get("type") == "thinking"]
    assert any(len(t) >= 90 and "Green Bowl" in t and "Sprout Table" in t
               for t in thought_texts)
    assert result["agent_events"][-1]["type"] == "card"
    assert result["agent_events"][-1]["card"]["poi"]["id"] == result["card"]["poi"]["id"]


def test_reject_returns_synchronized_agent_events_for_replacement_card():
    session, _llm, _bus = _session()
    first = session.start("preset query", source="preset", preset_id="family")["card"]["poi"]["id"]

    result = session.reject()

    assert result["card"]["poi"]["id"] != first
    assert result["agent_events"][0]["type"] == "user_action"
    assert "换一个" in result["agent_events"][0]["text"]
    assert result["agent_events"][-1]["type"] == "card"
    assert result["agent_events"][-1]["card"]["poi"]["id"] == result["card"]["poi"]["id"]


def test_execute_returns_synchronized_agent_events_for_one_click_booking():
    session, _llm, _bus = _session()
    session.start("preset query", source="preset", preset_id="family")
    session.accept()
    session.accept()
    session.accept()

    result = session.execute()

    assert result["results"]
    assert result["agent_delay_ms"] > 0
    assert result["agent_events"][0]["type"] == "user_action"
    assert "一键执行" in result["agent_events"][0]["text"]
    assert any(e.get("type") == "tool_call" for e in result["agent_events"])
    assert result["agent_events"][-1]["type"] == "execute_done"


def test_agent_thoughts_are_specific_and_not_generic_templates():
    session, _llm, _bus = _session()
    session.start("preset query", source="preset", preset_id="family")
    result = session.accept()

    thought_texts = [e.get("text", "") for e in result["agent_events"]
                     if e.get("type") == "thinking"]

    assert thought_texts
    assert all("它和当前段落目标更匹配，也能接住前面的选择" not in t
               for t in thought_texts)
    assert max(len(t) for t in thought_texts) >= 120


def test_preset_first_card_thoughts_are_customized_for_each_scene():
    forbidden = [
        "它和当前场景匹配度最高",
        "且下一段吃饭衔接方便",
        "我会按先玩、再吃、最后轻松收尾来排",
    ]

    for preset_id in ("family", "couple", "friends", "solo"):
        session, _llm, _bus = _session()
        result = session.start("preset query", source="preset", preset_id=preset_id)
        thought_texts = [
            e.get("text", "") for e in result["agent_events"]
            if e.get("type") == "thinking"
        ]

        assert any(len(t) >= 120 for t in thought_texts), preset_id
        assert all(bad not in "\n".join(thought_texts) for bad in forbidden)


def test_preset_agent_chain_exposes_parametric_memory_concept_outside_cards():
    session, _llm, _bus = _session()

    result = session.start("preset query", source="preset", preset_id="family")

    event_text = "\n".join(
        str(e.get("detail", "")) + "\n" + str(e.get("text", ""))
        for e in result["agent_events"]
    )
    card_text = "\n".join(
        str(result["card"].get(key, ""))
        for key in ("summary", "suggestion")
    )

    assert "LoRA+RL" in event_text
    assert "通用技能内化" in event_text
    assert "结构化事实记忆" in event_text
    assert "LoRA+RL" not in card_text
    assert "通用技能内化" not in card_text


def test_preset_replay_has_slower_thinking_and_tool_latency():
    session, _llm, _bus = _session()

    result = session.start("preset query", source="preset", preset_id="family")

    assert result["agent_delay_ms"] >= 220
    tool_calls = [e for e in result["agent_events"] if e.get("type") == "tool_call"]
    assert tool_calls
    assert all(e.get("delay_ms", 0) >= 400 for e in tool_calls)


def test_preset_followup_actions_keep_tool_latency():
    session, _llm, _bus = _session()
    session.start("preset query", source="preset", preset_id="family")

    result = session.accept()

    assert result["agent_delay_ms"] >= 160
    tool_calls = [e for e in result["agent_events"] if e.get("type") == "tool_call"]
    assert tool_calls
    assert all(e.get("delay_ms", 0) >= 400 for e in tool_calls)
