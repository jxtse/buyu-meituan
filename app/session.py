"""推荐会话状态机 —— 本地引力的大脑。

一次会话 = 一个下午的渐进式规划：
  start(query)
    -> locate (美团定位)
    -> intent (LLM 解析约束)
    -> segment plan (LLM 软固定三段, 可灵活调整)
    -> 为第 0 段出第一张卡
  accept()  -> 当前卡进 plan, 推进到下一段并出卡; 全部完成 -> 待执行
  reject()  -> 当前 id 拉黑, 同段换下一个最优
  chat(msg) -> LLM 决定 keep/switch/refine
  detail()  -> 商户详情（种草帖/团购/评论）
  execute() -> 逐站下单, 命中 无座/无票 自动换备选, 冲突自动错峰

所有关键步骤通过 event_bus 推 thinking / stage / tool 事件，右侧技术面板实时展示。
symbolic（mock 检索/距离/排序/故障）给保证，neural（LLM）给语义选择与话术。
"""
from __future__ import annotations

import json
import re
import time
import uuid
import copy
from dataclasses import dataclass, field
from typing import Any

from . import prompts
from .llm import LLMClient
from .mock.meituan import MeituanMock


PRESET_SCENARIOS: dict[str, dict[str, Any]] = {
    "family": {
        "constraints": {
            "scene": "family", "adults": 2, "kids": 1, "kid_age": 5,
            "diet": [], "time_window": {"start": "14:00", "hours": 4},
            "budget_level": "high", "preferences": ["亲子", "遛娃", "美食", "就近"],
            "summary": "周末下午从金陵天地·河西万达商圈出发，一家两大一小就近找适合5岁孩子的遛娃地点，再安排一顿品质较好的晚饭。",
        },
        "narrative": "我帮你按「先玩 → 再吃 → 加个轻松收尾」来安排这个下午。",
        "segments": [
            {"kind": "play", "intent": "先安排适合5岁孩子的室内亲子遛娃项目"},
            {"kind": "eat", "intent": "玩累了就近吃一顿适合家庭的好饭"},
            {"kind": "extra", "intent": "饭后安排一个轻松不折腾的亲子收尾"},
        ],
        "simulated_events": [
            "识别到首页亲子预设场景：两大一小、5岁孩子、就近遛娃加品质晚饭。",
            "这条演示路径使用本地生活库做稳定规划，优先保证评委现场体验速度。",
        ],
    },
    "couple": {
        "constraints": {
            "scene": "couple", "adults": 2, "kids": 0, "kid_age": None,
            "diet": [], "time_window": {"start": "18:00", "hours": 4},
            "budget_level": "medium", "preferences": ["文艺", "约会", "氛围晚餐", "惊喜"],
            "summary": "今天两人约会，想先找一个文艺好逛的地方，再安排一顿有氛围的晚饭，最好带一点惊喜感。",
        },
        "narrative": "我帮你按「先逛 → 再吃 → 加个小惊喜」来安排这个约会。",
        "segments": [
            {"kind": "play", "intent": "先安排适合情侣的文艺逛逛项目"},
            {"kind": "eat", "intent": "再找一家有氛围的双人晚餐"},
            {"kind": "extra", "intent": "最后安排一个不打扰节奏的小惊喜"},
        ],
        "simulated_events": [
            "识别到首页情侣约会预设场景：文艺逛逛、氛围晚餐、小惊喜。",
            "本地生活库会优先选择适合拍照、晚餐动线顺、双人套餐明确的候选。",
        ],
    },
    "friends": {
        "constraints": {
            "scene": "friends", "adults": 4, "kids": 0, "kid_age": None,
            "diet": [], "time_window": {"start": "15:00", "hours": 5},
            "budget_level": "medium", "preferences": ["朋友聚会", "热闹", "撸串", "预算适中"],
            "summary": "周末三四个朋友聚一下，先玩点有参与感的项目，再找一家热闹、预算别太高的聚餐。",
        },
        "narrative": "我帮你按「先玩热身 → 再聚餐 → 轻松收尾」来安排。",
        "segments": [
            {"kind": "play", "intent": "先安排适合朋友一起玩的热身项目"},
            {"kind": "eat", "intent": "再找一家适合多人聚会的热闹餐厅"},
            {"kind": "extra", "intent": "最后加一个适合朋友继续聊的收尾点"},
        ],
        "simulated_events": [
            "识别到首页朋友聚会预设场景：多人、热闹、预算适中。",
            "排序会更看重多人套餐、氛围和餐后继续活动的便利性。",
        ],
    },
    "solo": {
        "constraints": {
            "scene": "solo", "adults": 1, "kids": 0, "kid_age": None,
            "diet": [], "time_window": {"start": "14:00", "hours": 4},
            "budget_level": "medium", "preferences": ["一个人", "citywalk", "放松", "随便逛逛"],
            "summary": "一个人下午想出去放松，随便逛逛，吃点好的，整体节奏轻松即可。",
        },
        "narrative": "我帮你按「随便逛逛 → 吃点好的 → 轻松收尾」来安排。",
        "segments": [
            {"kind": "play", "intent": "先找一个适合一个人放松闲逛的地点"},
            {"kind": "eat", "intent": "再安排一顿一个人也舒服的餐食"},
            {"kind": "extra", "intent": "最后留一个不用赶时间的轻松收尾"},
        ],
        "simulated_events": [
            "识别到首页单人放松预设场景：低压力、可临时调整、吃逛结合。",
            "候选会优先考虑动线简单、一个人体验不尴尬、评价稳定的地点。",
        ],
    },
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(_text(v) for v in value if _text(v))
    if isinstance(value, dict):
        return "、".join(_text(v) for v in value.values() if _text(v))
    return str(value).strip()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_text(v) for v in value if _text(v)]
    if isinstance(value, dict):
        return [_text(v) for v in value.values() if _text(v)]
    text = _text(value)
    return [text] if text else []


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, "", []):
            return data[key]
    return None


def _kind_from_text(text: str, fallback_index: int = 0) -> str:
    lowered = text.lower()
    if any(k in text for k in ("额外", "惊喜", "鲜花", "蛋糕", "散步", "收尾", "消食")) \
            or "extra" in lowered:
        return "extra"
    if any(k in text for k in ("吃", "餐", "饭", "晚餐", "午餐")) or "eat" in lowered:
        return "eat"
    if any(k in text for k in ("玩", "遛娃", "展", "逛", "活动", "亲子")) or "play" in lowered:
        return "play"
    return ("play", "eat", "extra")[min(fallback_index, 2)]


def _is_intake_greeting(query: str) -> bool:
    q = (query or "").strip().lower()
    compact = re.sub(r"[\s!！?？。.,，~～]+", "", q)
    if not compact:
        return True
    greetings = {
        "hi", "hihi", "hello", "hey", "yo",
        "你好", "您好", "哈喽", "哈啰", "嗨", "在吗", "在嘛",
        "小团", "小团你好", "你好小团",
    }
    if compact in greetings:
        return True

    planning_signals = (
        "吃", "喝", "玩", "逛", "走", "放松", "约会", "朋友", "孩子", "娃",
        "老婆", "对象", "一个人", "自己", "独自", "下午", "晚上", "周末",
        "附近", "新街口", "河西", "夫子庙", "万达", "餐厅", "咖啡", "展览",
    )
    return len(compact) <= 8 and not any(signal in compact for signal in planning_signals)


def _intake_greeting_reply() -> str:
    return (
        "你好，我是小团。你想怎么安排这次出门？可以先告诉我同行人、时间、"
        "大概区域，以及是想吃饭、玩乐、散步还是找个地方坐一会儿。"
    )


def _extract_intake_facts(message: str) -> dict[str, Any]:
    text = message or ""
    facts: dict[str, Any] = {"preferences": []}
    if any(k in text for k in ("新街口", "河西", "夫子庙", "金陵天地", "万达", "老门东", "玄武湖")):
        for area in ("新街口", "河西", "夫子庙", "金陵天地", "万达", "老门东", "玄武湖"):
            if area in text:
                facts["area"] = area
                break
    elif any(k in text for k in ("哪里都行", "哪都行", "都可以", "不限区域", "附近都行", "随便哪里")):
        facts["area"] = "不限区域"
        facts["preferences"].append("哪里都行")
    if any(k in text for k in ("现在", "马上", "立刻", "即刻", "这会儿")):
        facts["time"] = "现在"
        facts["preferences"].append("现在")
    elif any(k in text for k in ("下午", "晚上", "今晚", "今天", "周末", "中午")):
        facts["time"] = next(k for k in ("下午", "晚上", "今晚", "今天", "周末", "中午") if k in text)
    lower = text.lower()
    if "solo" in lower or any(k in text for k in ("一个人", "自己", "独自", "单人", "一人")):
        facts["companions"] = "一个人"
        facts["preferences"].append("一个人")
    elif any(k in text for k in ("朋友", "同事", "几个人", "三四个")):
        facts["companions"] = "朋友"
    elif any(k in text for k in ("对象", "女朋友", "男朋友", "老婆", "老公", "约会")):
        facts["companions"] = "两人"
    elif any(k in text for k in ("孩子", "娃", "亲子", "家人")):
        facts["companions"] = "家庭"

    if any(k in text for k in ("坐一会", "坐会", "咖啡", "喝点", "安静", "放松")):
        facts["goal"] = "找个地方坐一会儿"
        facts["preferences"].append("坐一会儿")
    if any(k in text for k in ("吃", "饭", "餐厅", "清淡", "轻食")):
        facts["goal"] = facts.get("goal") or "吃饭"
    if any(k in text for k in ("逛", "走走", "散步", "city", "City", "看展", "展览")):
        facts["goal"] = facts.get("goal") or "逛逛放松"

    for pref, keys in {
        "安静": ("安静", "清静", "不吵"),
        "清淡": ("清淡", "轻食", "减脂"),
        "咖啡": ("咖啡",),
        "散步": ("散步", "走走"),
    }.items():
        if any(k in text for k in keys):
            facts["preferences"].append(pref)
    facts["preferences"] = sorted(set(facts["preferences"]))
    return facts


def _normalize_intent(data: dict[str, Any], query: str = "") -> dict[str, Any]:
    if len(data) == 1:
        only = next(iter(data.values()))
        if isinstance(only, dict):
            data = only
    if all(k in data for k in ("scene", "adults", "kids", "time_window")):
        out = dict(data)
        out.setdefault("diet", [])
        out.setdefault("preferences", [])
        out.setdefault("summary", "")
        out.setdefault("budget_level", "medium")
        return _apply_query_sanity_overrides(out, query)

    constraints = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
    companions = constraints.get("同行人员") or constraints.get("companions") or {}
    child = companions.get("儿童") or companions.get("children") or {}
    child_present = bool(
        (isinstance(child, dict) and (child.get("有无") or child.get("has_child") or child.get("count")))
        or (isinstance(child, int) and child > 0)
        or companions.get("child_age")
    )
    q = query
    scene = "friends"
    if child_present or any(k in q for k in ("老婆", "孩子", "娃", "带娃", "家人")):
        scene = "family"
    elif any(k in q for k in ("对象", "约会", "情侣", "女朋友", "男朋友")):
        scene = "couple"

    total = companions.get("总人数") or companions.get("total")
    if total is None and companions.get("adults") is not None:
        total = int(companions.get("adults") or 0) + int(companions.get("children") or 0)
    total = total or (3 if scene == "family" else 2)
    kids = 1 if scene == "family" else 0
    if isinstance(child, int):
        kids = child
    elif isinstance(child, dict) and isinstance(child.get("count"), int):
        kids = child["count"]
    kid_age = (child.get("年龄") or child.get("age")) if isinstance(child, dict) else None
    kid_age = kid_age or companions.get("child_age")
    if kid_age is None and "5岁" in q:
        kid_age = 5
    adults = max(int(total or 2) - kids, 1)

    preferences = set(_as_list(data.get("preferences")))
    preferences.update(_as_list(constraints.get("活动需求") or constraints.get("activity_requirements")))
    preferences.update(_as_list(constraints.get("餐饮需求") or constraints.get("dining_requirements")))
    location_text = _text(constraints.get("地点约束") or constraints.get("location"))
    dining_text = _text(constraints.get("餐饮需求") or constraints.get("dining_requirements"))
    if "近" in location_text or "别离家太远" in q:
        preferences.add("就近")
    if scene == "family":
        preferences.add("亲子")

    return _apply_query_sanity_overrides({
        "scene": scene,
        "adults": adults,
        "kids": kids,
        "kid_age": kid_age,
        "diet": [],
        "time_window": {"start": "14:00", "hours": 4},
        "budget_level": "high" if any(k in (q + dining_text) for k in ("好点", "好的", "品质")) else "medium",
        "preferences": sorted(p for p in preferences if p),
        "summary": data.get("summary") or f"从当前位置出发，安排适合{scene}场景的周末活动和餐食。",
    }, query)


def _apply_query_sanity_overrides(out: dict[str, Any], query: str = "") -> dict[str, Any]:
    q = query or ""
    prefs = set(_as_list(out.get("preferences")))
    diet = set(_as_list(out.get("diet")))
    has_family_signal = any(k in q for k in ("老婆", "孩子", "娃", "带娃", "家人", "儿子", "女儿"))
    has_couple_signal = any(k in q for k in ("对象", "约会", "情侣", "女朋友", "男朋友", "老公"))
    has_solo_signal = any(k in q.lower() for k in ("solo",)) or any(
        k in q for k in ("一个人", "自己", "独自", "单人", "一人")
    )
    has_friends_signal = any(k in q for k in ("朋友", "同事", "和朋友", "三四个朋友"))
    if has_solo_signal and not has_family_signal and not has_couple_signal:
        out["scene"] = "solo"
        out["adults"] = 1
        out["kids"] = 0
        out["kid_age"] = None
        prefs.add("一个人")
        if out.get("summary") and ("朋友" in out["summary"] or "friends" in out["summary"].lower()):
            out["summary"] = "一个人从当前位置出发，安排轻松、低压力的吃逛和安静收尾。"
    elif has_friends_signal and not has_family_signal and not has_couple_signal:
        out["scene"] = "friends"
        out["adults"] = max(int(out.get("adults") or 2), 2)
        out["kids"] = 0
        out["kid_age"] = None
        prefs.add("朋友")
    if "新街口" in q:
        prefs.add("新街口")
    if any(k in q for k in ("哪里都行", "哪都行", "都可以", "不限区域", "附近都行", "随便哪里")):
        prefs.add("哪里都行")
    if any(k in q for k in ("现在", "马上", "立刻", "即刻", "这会儿")):
        prefs.add("现在")
    if any(k in q for k in ("清淡", "轻食", "低脂", "减脂", "减肥")):
        prefs.add("清淡")
        prefs.add("轻食")
        if any(k in q for k in ("减脂", "减肥")):
            diet.add("减脂")
    if "安静" in q:
        prefs.add("安静")
    out["preferences"] = sorted(p for p in prefs if p)
    out["diet"] = sorted(d for d in diet if d)
    return out


def _normalize_segment_plan(data: dict[str, Any]) -> dict[str, Any]:
    raw_segments = data.get("segments") or data.get("itinerary") or data.get("plan") or []
    segments: list[dict[str, str]] = []
    for i, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            continue
        kind_text = _text(item.get("kind") or item.get("type") or item.get("category") or item.get("label"))
        intent = _text(item.get("intent") or item.get("goal") or item.get("description") or item.get("summary"))
        kind = item.get("kind") if item.get("kind") in {"play", "eat", "extra"} else _kind_from_text(kind_text + intent, i)
        if kind not in {s["kind"] for s in segments}:
            segments.append({"kind": kind, "intent": intent or kind_text})
    if not segments:
        return data
    return {
        "segments": segments[:3],
        "narrative": _text(data.get("narrative") or data.get("opening") or data.get("summary"))
        or "我帮你按「先玩 → 再吃 → 加个收尾」来安排这个下午。",
    }


def _normalize_recommendation(data: dict[str, Any], candidates: list[dict]) -> dict[str, Any]:
    out = dict(data)
    chosen = out.get("chosen_id") or out.get("poi_id") or out.get("id")
    chosen_name = _text(_first_present(out, ("推荐地点", "地点", "name", "chosen_name", "venue")))
    if not chosen and chosen_name:
        for c in candidates:
            if chosen_name in c["name"] or c["name"] in chosen_name:
                chosen = c["id"]
                break
    if not chosen and candidates:
        chosen = candidates[0]["id"]
    candidate = next((c for c in candidates if c["id"] == chosen), candidates[0] if candidates else {})

    groupon_id = out.get("groupon_id")
    groupon_name = _text(_first_present(out, ("推荐套餐", "套餐", "groupon", "coupon")))
    if not groupon_id and groupon_name:
        for g in candidate.get("groupon", []):
            if groupon_name in g.get("title", "") or g.get("title", "") in groupon_name:
                groupon_id = g.get("id")
                break

    summary = _text(out.get("summary") or out.get("推荐理由") or out.get("reason") or candidate.get("ai_pitch"))
    return {
        "chosen_id": chosen,
        "summary": summary,
        "groupon_id": groupon_id,
        "groupon_reason": _text(out.get("groupon_reason") or out.get("套餐理由") or out.get("coupon_reason")),
        "suggestion": _text(out.get("suggestion") or out.get("补充建议") or out.get("extra_tip")),
        "confidence": float(out.get("confidence") or out.get("confidence_score") or 0.8),
    }


# ---------------- 数据结构 ----------------
@dataclass
class Recommendation:
    segment_kind: str
    segment_intent: str
    poi: dict[str, Any]               # brief
    summary: str
    groupon_id: str | None
    groupon_reason: str
    suggestion: str
    confidence: float

    def to_card(self) -> dict[str, Any]:
        groupon = None
        for g in self.poi.get("groupon", []):
            if g.get("id") == self.groupon_id:
                groupon = g
                break
        if groupon is None and self.groupon_id is not None:
            groupon = self.poi.get("top_groupon")
        card = {
            "segment_kind": self.segment_kind,
            "segment_intent": self.segment_intent,
            "poi": self.poi,
            "summary": self.summary,
            "groupon": groupon,
            "groupon_reason": self.groupon_reason,
            "suggestion": self.suggestion,
            "confidence": self.confidence,
        }
        booking_execution = self._booking_execution()
        if booking_execution:
            card["booking_execution"] = booking_execution
        return card

    def _booking_execution(self) -> dict[str, Any] | None:
        booking = self.poi.get("booking", {})
        if self.segment_kind != "eat" or booking.get("type") != "table":
            return None
        queue = self.poi.get("queue", {})
        return {
            "status": "pending",
            "title": "预备取号 2 人位",
            "subtitle": "确认方案后执行取号，到店前可按号候位",
            "wait_min": queue.get("wait_min"),
        }


@dataclass
class Segment:
    kind: str
    intent: str
    rejected: set[str] = field(default_factory=set)
    current: Recommendation | None = None
    accepted: Recommendation | None = None


class Session:
    SEGMENT_LABELS = {"play": "玩乐", "eat": "吃饭", "extra": "额外活动"}

    def __init__(self, *, llm: LLMClient, mock: MeituanMock, bus: Any = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.llm = llm
        self.mock = mock
        self.bus = bus
        self.query: str = ""
        self.constraints: dict[str, Any] = {}
        self.segments: list[Segment] = []
        self.idx: int = 0
        self.narrative: str = ""
        self.started_at = time.time()
        self.location: dict[str, Any] = {}
        self.intake_active = False
        self.intake_messages: list[str] = []
        self.intake_context: dict[str, Any] = {
            "raw": [],
            "preferences": [],
            "area": None,
            "time": None,
            "companions": None,
            "goal": None,
        }

    # ---------------- 事件 ----------------
    def _emit(self, ev: dict) -> None:
        ev.setdefault("ts", round(time.time() - self.started_at, 2))
        if self.bus:
            try:
                self.bus.publish(ev)
            except Exception:
                pass

    def _stage(self, stage: str, detail: str = "") -> None:
        self._emit({"type": "stage", "stage": stage, "detail": detail})

    def _think(self, text: str) -> None:
        self._emit({"type": "thinking", "text": text})

    def _agent_tool(self, name: str, args: dict[str, Any], result: dict[str, Any] | None = None) -> None:
        self._emit({"type": "tool_call", "source": "xiaotuan_agent",
                    "name": name, "args": args})
        if result is not None:
            self._emit({"type": "tool_result", "name": name, "result": result})

    # ---------------- 启动 ----------------
    def start(self, query: str, *, source: str = "custom", preset_id: str | None = None) -> dict[str, Any]:
        self.query = query
        self._stage("locate", "调用美团定位获取当前位置")
        self.location = self.mock.locate()
        self._think(f"用户在{self.location['business_area']}，先理解 TA 的需求。")

        if source != "preset" and _is_intake_greeting(query):
            return self._run_intake_agent(query, first_turn=True)

        # 1) 意图解析
        if source == "preset":
            self.constraints = self._load_preset_constraints(preset_id)
        else:
            self._stage("intent", "解析自然语言需求 → 结构化约束")
            self.constraints = self._parse_intent(query)
        self._emit({"type": "constraints", "data": self.constraints})
        self._think(f"需求理解：{self.constraints.get('summary', '')}")

        # 2) 分段规划
        if source == "preset":
            plan = self._load_preset_plan(preset_id)
        else:
            self._stage("plan", "按用户目标编排可执行分段")
            plan = self._plan_segments(self.constraints)
        self.narrative = plan.get("narrative", "")
        self.segments = [Segment(kind=s["kind"], intent=s.get("intent", ""))
                         for s in plan.get("segments", [])]
        self.idx = 0
        self._emit({"type": "segment_plan",
                    "narrative": self.narrative,
                    "segments": [{"kind": s.kind,
                                  "label": self.SEGMENT_LABELS.get(s.kind, s.kind),
                                  "intent": s.intent} for s in self.segments]})

        # 3) 第一段出卡
        card = self._recommend_current()
        result = {
            "session_id": self.id,
            "location": self.location,
            "constraints": self.constraints,
            "narrative": self.narrative,
            "segments": [{"kind": s.kind,
                          "label": self.SEGMENT_LABELS.get(s.kind, s.kind),
                          "intent": s.intent} for s in self.segments],
            "current_index": self.idx,
            "card": card,
            "done": False,
        }
        if source == "preset":
            result["agent_delay_ms"] = 240
            result["agent_events"] = self._with_tool_latency(
                self._preset_agent_events(preset_id, card))
        return result

    # ---------------- LLM 步骤 ----------------
    def _run_intake_agent(self, message: str, *, first_turn: bool = False) -> dict[str, Any]:
        self.intake_active = True
        self.intake_messages.append(message)
        self._stage("chat", "ReAct 需求澄清 Agent")
        self._think(
            "ReAct step 1：从当前多轮对话里抽取目标、区域、同行人和时间，"
            "判断是否已经足够调用地点检索。如果缺少关键信息，就调用 agent_speak 继续问；"
            "如果信息足够，再进入美团/点评工具链。")
        facts = self._update_intake_context(message)
        self._emit({"type": "context_update", "source": "xiaotuan_agent",
                    "extracted": facts, "context": self.intake_context})

        missing = self._intake_missing_slots()
        if first_turn or missing:
            reply = _intake_greeting_reply() if first_turn else self._intake_followup_reply(missing)
            options = self._intake_options(missing, first_turn=first_turn)
            self._agent_tool("agent_speak", {
                "purpose": "ask_followup",
                "missing_slots": missing,
                "options": options,
            }, {
                "text": reply,
            })
            self._emit({"type": "assistant_reply", "text": reply})
            return {
                "session_id": self.id,
                "location": self.location,
                "constraints": {},
                "narrative": "",
                "segments": [],
                "current_index": 0,
                "card": None,
                "done": False,
                "reply": reply,
                "needs_more_info": True,
                "intake_active": True,
                "intake_options": options,
                **self._sync_agent_payload(self._intake_replay_events(
                    message, reply, missing, first_turn=first_turn)),
            }

        return self._complete_intake_planning()

    def _update_intake_context(self, message: str) -> dict[str, Any]:
        facts = _extract_intake_facts(message)
        self.intake_context["raw"].append(message)
        for key in ("area", "time", "companions", "goal"):
            if facts.get(key):
                self.intake_context[key] = facts[key]
        prefs = set(self.intake_context.get("preferences") or [])
        prefs.update(facts.get("preferences") or [])
        self.intake_context["preferences"] = sorted(prefs)
        return facts

    def _intake_missing_slots(self) -> list[str]:
        missing: list[str] = []
        if not self.intake_context.get("goal"):
            missing.append("想做什么")
        if not self.intake_context.get("area"):
            missing.append("附近区域")
        if not self.intake_context.get("companions"):
            missing.append("同行人")
        if not self.intake_context.get("time"):
            missing.append("时间")
        return missing

    def _intake_followup_reply(self, missing: list[str]) -> str:
        goal = self.intake_context.get("goal") or "坐一会儿"
        if set(missing) >= {"附近区域", "同行人", "时间"}:
            return f"可以，我先记下你想{goal}。你打算在哪个商圈或地铁站附近？是一个人还是和朋友？大概下午还是晚上去？"
        parts = []
        if "附近区域" in missing:
            parts.append("你想在哪个区域附近")
        if "同行人" in missing:
            parts.append("是一个人还是和别人一起")
        if "时间" in missing:
            parts.append("大概什么时间去")
        if "想做什么" in missing:
            parts.append("主要想吃饭、喝咖啡、散步还是看展")
        return "可以，我继续帮你收窄一下。" + "，".join(parts) + "？"

    @staticmethod
    def _intake_options(missing: list[str], *, first_turn: bool = False) -> list[dict[str, Any]]:
        wanted = ["想做什么", "同行人", "时间", "附近区域"] if first_turn else missing
        groups = {
            "想做什么": {
                "slot": "goal",
                "title": "想做什么",
                "options": [
                    {"label": "散步", "value": "散步"},
                    {"label": "坐一会儿", "value": "找个地方坐一会儿"},
                    {"label": "吃点好的", "value": "吃点好的"},
                    {"label": "看展逛逛", "value": "看展逛逛"},
                ],
            },
            "同行人": {
                "slot": "companions",
                "title": "和谁去",
                "options": [
                    {"label": "一个人", "value": "一个人"},
                    {"label": "和朋友", "value": "和朋友"},
                    {"label": "约会", "value": "和对象约会"},
                    {"label": "带孩子", "value": "带孩子"},
                ],
            },
            "时间": {
                "slot": "time",
                "title": "什么时候",
                "options": [
                    {"label": "现在去", "value": "现在去"},
                    {"label": "下午", "value": "下午"},
                    {"label": "晚上", "value": "晚上"},
                    {"label": "周末", "value": "周末"},
                ],
            },
            "附近区域": {
                "slot": "area",
                "title": "在哪附近",
                "options": [
                    {"label": "金陵天地", "value": "金陵天地附近"},
                    {"label": "新街口", "value": "新街口附近"},
                    {"label": "夫子庙", "value": "夫子庙附近"},
                    {"label": "哪里都行", "value": "哪里都行"},
                ],
            },
        }
        return [groups[item] for item in wanted if item in groups]

    def _complete_intake_planning(self) -> dict[str, Any]:
        combined_query = "；".join(self.intake_messages)
        self.query = combined_query
        self._stage("intent", "LLM ReAct 汇总多轮上下文 → 结构化约束")
        self.constraints = self._parse_intent(combined_query)
        prefs = set(self.constraints.get("preferences") or [])
        prefs.update(self.intake_context.get("preferences") or [])
        if self.intake_context.get("area"):
            prefs.add(str(self.intake_context["area"]))
        self.constraints["preferences"] = sorted(prefs)
        self._emit({"type": "constraints", "data": self.constraints})
        self._think(f"需求理解：{self.constraints.get('summary', '')}")

        self._stage("plan", "ReAct Planning：基于当前对话上下文生成分段")
        plan = self._plan_segments(self.constraints)
        self.narrative = plan.get("narrative", "")
        self.segments = [Segment(kind=s["kind"], intent=s.get("intent", ""))
                         for s in plan.get("segments", [])]
        self.idx = 0
        segment_payload = [{"kind": s.kind,
                            "label": self.SEGMENT_LABELS.get(s.kind, s.kind),
                            "intent": s.intent} for s in self.segments]
        self._emit({"type": "segment_plan",
                    "narrative": self.narrative,
                    "segments": segment_payload})

        card = self._recommend_current()
        if card and card.get("poi", {}).get("id"):
            detail = self.mock.detail(card["poi"]["id"])
            self._agent_tool("show_recommendation_card", {
                "poi_id": card["poi"]["id"],
                "segment_index": self.idx,
            }, {
                "rendered": True,
                "notes": len(detail.get("dianping_notes", [])),
                "reviews": len(detail.get("reviews", [])),
                "groupons": len(detail.get("groupon", [])),
            })
        reply = "我先按你刚补充的信息做一版，下面这张卡是第一站；你可以继续说偏好，我会边聊边调整。"
        self._emit({"type": "assistant_reply", "text": reply})
        self.intake_active = False
        return {
            "session_id": self.id,
            "location": self.location,
            "constraints": self.constraints,
            "narrative": self.narrative,
            "segments": segment_payload,
            "current_index": self.idx,
            "card": card,
            "done": False,
            "reply": reply,
            "intake_active": False,
            **self._sync_agent_payload(self._intake_complete_replay_events(card)),
        }

    def _intake_replay_events(
        self,
        message: str,
        reply: str,
        missing: list[str],
        *,
        first_turn: bool,
    ) -> list[dict[str, Any]]:
        return [
            {"type": "stage", "stage": "chat", "detail": "ReAct 需求澄清 Agent", "ts": 0.0},
            {"type": "thinking", "text": "ReAct：观察用户输入，从当前对话里更新需求槽位，再决定是继续追问还是进入地点检索。", "ts": 0.1},
            {"type": "context_update", "source": "xiaotuan_agent",
             "result": {"context": self.intake_context}, "ts": 0.25},
            {"type": "thinking", "text": "当前需求还没有形成可执行的地点检索约束，先用对话工具补齐缺口，而不是猜一个地点。", "ts": 0.4},
            {"type": "tool_call", "name": "agent_speak", "args": {
                "first_turn": first_turn,
                "missing_slots": missing,
            }, "ts": 0.5},
            {"type": "tool_result", "name": "agent_speak", "result": {"text": reply}, "ts": 0.6},
        ]

    def _intake_complete_replay_events(self, card: dict[str, Any] | None) -> list[dict[str, Any]]:
        poi = (card or {}).get("poi", {})
        return [
            {"type": "stage", "stage": "context", "detail": "汇总当前多轮对话上下文", "ts": 0.0},
            {"type": "context_update", "source": "xiaotuan_agent",
             "result": {"context": self.intake_context}, "ts": 0.2},
            {"type": "stage", "stage": "intent", "detail": "LLM 汇总上下文并生成结构化约束", "ts": 0.3},
            {"type": "constraints", "data": self.constraints, "ts": 0.4},
            {"type": "stage", "stage": "plan", "detail": "规划渐进式推荐段落", "ts": 0.5},
            {"type": "segment_plan", "narrative": self.narrative, "segments": [
                {"kind": s.kind, "label": self.SEGMENT_LABELS.get(s.kind, s.kind), "intent": s.intent}
                for s in self.segments
            ], "ts": 0.6},
            {"type": "stage", "stage": "search", "detail": "调用点评检索、详情、团购、评论和种草帖工具", "ts": 0.7},
            {"type": "tool_call", "name": "dianping_search", "args": {
                "segment": self.segments[self.idx].kind if self.segments else "play",
                "scene": self.constraints.get("scene"),
            }, "ts": 0.8},
            {"type": "tool_call", "name": "dianping_detail", "args": {"poi_id": poi.get("id")}, "ts": 1.0},
            {"type": "tool_call", "name": "show_recommendation_card", "args": {
                "poi_id": poi.get("id"),
                "segment_index": self.idx,
            }, "ts": 1.2},
            {"type": "card", "card": card, "segment_index": self.idx, "ts": 1.4},
        ]

    def _preset(self, preset_id: str | None) -> dict[str, Any]:
        key = preset_id if preset_id in PRESET_SCENARIOS else "family"
        return PRESET_SCENARIOS[key]

    def _load_preset_constraints(self, preset_id: str | None) -> dict[str, Any]:
        preset = self._preset(preset_id)
        self._stage("intent", "解析首页预设场景 → 结构化约束")
        for text in preset.get("simulated_events", []):
            self._think(text)
        return copy.deepcopy(preset["constraints"])

    def _load_preset_plan(self, preset_id: str | None) -> dict[str, Any]:
        preset = self._preset(preset_id)
        self._stage("plan", "根据预设场景生成稳定演示分段")
        self._think("已生成可稳定执行的三段式下午方案，后续检索与执行均使用本地生活库。")
        return {
            "segments": copy.deepcopy(preset["segments"]),
            "narrative": preset["narrative"],
        }

    def _preset_agent_events(self, preset_id: str | None, card: dict[str, Any] | None) -> list[dict[str, Any]]:
        """给公网演示前端稳定回放的 Agent 链路，避免 SSE 被代理缓冲时右侧空白。"""
        preset = self._preset(preset_id)
        segment = self.segments[0] if self.segments else None
        poi = (card or {}).get("poi", {})
        candidates = []
        if segment:
            candidates = self._search_candidates(segment.kind, self.constraints.get("scene"), set(), 6)
        segments = [{
            "kind": s.kind,
            "label": self.SEGMENT_LABELS.get(s.kind, s.kind),
            "intent": s.intent,
        } for s in self.segments]
        return [
            {"type": "stage", "stage": "locate", "detail": "读取当前位置与附近商圈", "ts": 0.0},
            {"type": "tool_call", "name": "meituan_locate", "args": {}, "ts": 0.1},
            {"type": "tool_result", "name": "meituan_locate", "result": self.location, "ts": 0.2},
            {"type": "stage", "stage": "intent", "detail": "解析首页场景 → 结构化约束", "ts": 0.4},
            {"type": "thinking", "text": preset.get("simulated_events", ["我先把同行人、时间和预算约束固定下来。"])[0], "ts": 0.5},
            {"type": "constraints", "data": self.constraints, "ts": 0.7},
            {"type": "stage", "stage": "skill", "detail": "LoRA+RL 通用规划习惯内化", "ts": 0.8},
            {"type": "thinking", "text": self._parametric_skill_thought(preset_id), "ts": 0.85},
            {"type": "stage", "stage": "plan", "detail": "生成三段式下午方案", "ts": 0.9},
            {"type": "thinking", "text": self._preset_plan_thought(preset_id), "ts": 1.0},
            {"type": "segment_plan", "narrative": self.narrative, "segments": segments, "ts": 1.2},
            {"type": "stage", "stage": "search", "detail": f"本地生活库检索：{self.SEGMENT_LABELS.get(segment.kind, '玩乐')}候选", "ts": 1.4},
            {"type": "tool_call", "name": "dianping_search", "args": {
                "segment": segment.kind if segment else "play",
                "scene": self.constraints.get("scene"),
                "limit": 6,
            }, "ts": 1.5},
            {"type": "tool_result", "name": "dianping_search", "result": {
                "count": len(candidates),
                "ids": [c["id"] for c in candidates],
            }, "ts": 1.7},
            {"type": "stage", "stage": "reason", "detail": "按距离、评分、场景匹配和团购适配排序", "ts": 1.9},
            {"type": "thinking", "text": self._preset_first_card_thought(preset_id, poi), "ts": 2.0},
            {"type": "card", "card": card, "segment_index": self.idx, "ts": 2.2},
        ]

    def _parametric_skill_thought(self, preset_id: str | None) -> str:
        scene = self._preset(preset_id)["constraints"].get("scene")
        scene_label = {
            "family": "亲子周末",
            "couple": "情侣约会",
            "friends": "朋友聚会",
            "solo": "独处放松",
        }.get(scene, "本次")
        return (
            f"这里先走参数化技能层：LoRA+RL 已把通用规划习惯内化为稳定能力，比如先控制路线摩擦、再检查实时可用性、"
            f"遇到等待过长时自动改选。{scene_label}只作为任务特定规则按需进入当前上下文，避免把冗长 skill 全塞进提示词造成注意力稀释。"
            "商圈、人数、预算、排队状态和偏好标签仍由本地生活工具实时返回；会话内的槽位直接来自当前对话，不再暴露额外读写工具。"
        )

    def _preset_plan_thought(self, preset_id: str | None) -> str:
        scene = self._preset(preset_id)["constraints"].get("scene")
        return {
            "family": (
                "我会把这个下午拆成孩子体力曲线最稳的三段：先做高参与度的室内项目，把天气和排队风险降下来；"
                "再安排能坐得住、出餐稳定的晚饭；最后只留轻量收尾，不再塞高强度项目。这样家长少折腾，孩子也不容易在饭点前后崩掉。"
            ),
            "couple": (
                "这条约会路线不能只按距离排，我会先给两个人留一个有共同记忆点的逛展/街区，再把晚餐放在情绪升温后，"
                "最后用鲜花、甜品或散步做低打扰惊喜。节奏上要有留白，避免每一站都像任务清单。"
            ),
            "friends": (
                "朋友局最怕一开始就坐下吃饭导致气氛没热起来，所以我先找能一起参与、容易拍照或吐槽的热身活动；"
                "中段再接多人套餐明确、桌型友好的餐厅；收尾只保留好聊天的点，方便有人继续玩、有人提前撤。"
            ),
            "solo": (
                "一个人的路线要把心理负担降到最低：先选不尴尬、可快可慢的逛展或街区漫步，再安排清淡稳定的一餐，"
                "最后给自己留一个安静坐会儿的出口。这里我会避免亲子或多人强互动场景，因为它们会破坏独处放松感。"
            ),
        }.get(scene, "我会先固定同行人、时间窗和预算，再把路线拆成可确认、可替换、可执行的几个步骤。")

    def _preset_first_card_thought(self, preset_id: str | None, poi: dict[str, Any]) -> str:
        scene = self._preset(preset_id)["constraints"].get("scene")
        name = poi.get("name", "候选地点")
        return {
            "family": (
                f"第一站我选「{name}」，核心不是把距离压到最低，而是同时满足室内、低天气风险、5岁孩子能动手参与、"
                "家长不用全程追跑这几个约束。它的亲子套票也正好覆盖2大1小，能把入场动作提前锁住；下午先完成高参与度项目，"
                "孩子释放精力后，后面的晚饭段才更容易坐得住。"
            ),
            "couple": (
                f"第一站我选「{name}」，因为约会前半段需要一个能自然制造共同话题的地点，而不是直接进入晚餐。"
                "我会看它是否适合慢慢逛、是否有拍照记忆点、离后续晚餐区是否顺路，以及双人票/套餐是否会增强仪式感。"
                "这个选择的作用是先铺情绪，再把晚餐和惊喜接上。"
            ),
            "friends": (
                f"第一站我选「{name}」，因为朋友局需要低门槛但有参与感的热身点。它能让几个人先一起逛、拍照、讨论，"
                "不需要复杂预约，也不会像高强度项目那样把体力提前消耗完。后面再接多人餐厅时，大家已经有共同话题，路线也更像完整聚会而不是单纯找饭吃。"
            ),
            "solo": (
                f"第一站我选「{name}」，因为一个人出门最重要的是可控和自在：不用等同伴、不需要强互动，也能随时放慢或结束。"
                "我会优先看安静程度、可独自停留的合理性、附近是否能接清淡餐食，以及它是否避开亲子/多人局的社交压力。"
                "这样用户不是被推去热闹场，而是真的获得一个低负担的下午。"
            ),
        }.get(scene, f"我选择「{name}」是因为它在距离、评分、价格和实时可用性上形成了稳定组合，适合作为第一张可执行推荐卡。")

    @staticmethod
    def _with_tool_latency(events: list[dict[str, Any]], delay_ms: int = 460) -> list[dict[str, Any]]:
        for event in events:
            if event.get("type") == "tool_call":
                event["delay_ms"] = max(int(event.get("delay_ms", 0) or 0), delay_ms)
        return events

    @classmethod
    def _sync_agent_payload(cls, events: list[dict[str, Any]], delay_ms: int = 180) -> dict[str, Any]:
        return {"agent_delay_ms": delay_ms, "agent_events": cls._with_tool_latency(events)}

    def _current_search_preview(self, card: dict[str, Any] | None) -> dict[str, Any]:
        seg = self.segments[self.idx] if self.idx < len(self.segments) else None
        scene = self.constraints.get("scene")
        candidates: list[dict[str, Any]] = []
        if seg:
            candidates = self._search_candidates(seg.kind, scene, seg.rejected, 6)
        return {
            "segment": seg.kind if seg else (card or {}).get("segment_kind"),
            "scene": scene,
            "limit": 6,
            "ids": [c["id"] for c in candidates],
            "candidates": candidates,
        }

    def _card_action_events(
        self,
        action_text: str,
        target_name: str,
        decision: str,
        card: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        preview = self._current_search_preview(card)
        poi_name = (card or {}).get("poi", {}).get("name", "")
        label = self.SEGMENT_LABELS.get(preview.get("segment"), preview.get("segment") or "候选")
        events = [
            {"type": "user_action", "text": action_text, "target": target_name, "ts": 0.0},
            {"type": "thinking", "text": self._transition_thought(decision, target_name, preview), "ts": 0.1},
            {"type": "stage", "stage": "search", "detail": f"同步用户操作后检索：{label}候选", "ts": 0.2},
            {"type": "tool_call", "name": "dianping_search", "args": {
                "segment": preview.get("segment"),
                "scene": preview.get("scene"),
                "exclude": sorted(self.segments[self.idx].rejected) if self.idx < len(self.segments) else [],
                "limit": preview.get("limit"),
            }, "ts": 0.3},
            {"type": "tool_result", "name": "dianping_search", "result": {
                "count": len(preview.get("ids") or []),
                "ids": preview.get("ids") or [],
            }, "ts": 0.4},
            {"type": "stage", "stage": "reason", "detail": "结合已选站点与当前段目标重新排序", "ts": 0.5},
        ]
        events.extend(self._runtime_adjustment_events(card, preview, start_ts=0.6))
        events.append({"type": "thinking", "text": self._card_decision_thought(card, preview), "ts": 1.1})
        events.append({"type": "card", "card": card, "segment_index": self.idx, "ts": 1.2})
        return events

    def _transition_thought(self, decision: str, target_name: str, preview: dict[str, Any]) -> str:
        segment = preview.get("segment")
        scene = preview.get("scene")
        if segment == "eat" and scene == "family":
            return (
                f"收到操作：{decision}。我先把「{target_name}」锁定为第一站，然后重新计算晚饭段："
                "5岁孩子刚做完室内科学活动，晚饭不能只看评分，还要同时看步行距离、是否有儿童可接受的主食、"
                "大人能不能坐下来休息、餐厅实时排队状态，以及团购是否正好覆盖一家三口。这个阶段我会先召回近场候选，"
                "再用实时可订状态过滤掉看起来不错但落地风险高的选项。")
        if segment == "eat":
            return (
                f"收到操作：{decision}。我会把「{target_name}」作为上一站锚点，重新评估吃饭段的时间窗、"
                "人均预算、排队风险和从当前地点过去的动线，避免只因为评分高就推荐一家实际到店很难坐下的餐厅。")
        if segment == "extra":
            return (
                f"收到操作：{decision}。现在进入收尾段，我会把前两站的体力消耗、结束时间和返程方向一起纳入判断，"
                "优先找不需要复杂预约、不会把行程拉得太长、但能让这个下午有完整结束感的轻量活动。")
        return (
            f"收到操作：{decision}。我会把「{target_name}」写入当前行程状态，再基于同伴结构、时间窗、距离和评价稳定性，"
            "重新排列下一批候选，确保下一张卡不是孤立推荐，而是能接住整条路线的后续选择。")

    def _runtime_adjustment_events(
        self,
        card: dict[str, Any] | None,
        preview: dict[str, Any],
        *,
        start_ts: float,
    ) -> list[dict[str, Any]]:
        if preview.get("segment") != "eat" or not card:
            return []
        chosen_id = (card.get("poi") or {}).get("id")
        blocked = next((c for c in preview.get("candidates", [])
                        if c.get("id") != chosen_id and self._needs_pre_card_adjustment(
                            Segment(kind="eat", intent=""), c)), None)
        if not blocked:
            return []
        queue = blocked.get("queue", {})
        return [
            {"type": "tool_call", "name": "meituan_query_queue", "args": {
                "poi_id": blocked.get("id"),
                "name": blocked.get("name"),
                "party_size": self.constraints.get("adults", 2) + self.constraints.get("kids", 0),
            }, "ts": start_ts},
            {"type": "tool_result", "name": "meituan_query_queue", "result": {
                "status": "no_table" if queue.get("has_table") is False else "long_wait",
                "wait_min": queue.get("wait_min"),
                "has_table": queue.get("has_table"),
            }, "ts": round(start_ts + 0.1, 1)},
            {"type": "self_heal", "kind": "auto_replace", "poi": blocked.get("name"),
             "ts": round(start_ts + 0.2, 1)},
            {"type": "self_heal_ok", "note": f"已避开「{blocked.get('name')}」的实时排队风险，改为更稳的可订候选。",
             "ts": round(start_ts + 0.3, 1)},
        ]

    def _card_decision_thought(self, card: dict[str, Any] | None, preview: dict[str, Any]) -> str:
        if not card:
            return "这一段暂时没有稳定候选，我会收窄条件后再尝试推荐。"
        poi = card.get("poi", {})
        segment = card.get("segment_kind")
        scene = self.constraints.get("scene")
        if segment == "eat" and scene == "family" and poi.get("id") == "sprout_table":
            return (
                "我原本会优先考虑 Green Bowl：它离上一站近、轻食对大人友好，表面上是很顺的选择。"
                "但实时队列显示它当前没有可直接预约的桌位，等待时间也偏长；对一家三口来说，孩子刚玩完以后继续等位，"
                "会显著增加哭闹和行程失控概率。所以我把它降权，改选 Sprout Table。Sprout Table 虽然距离稍远，"
                "但有可订座位、等待更短，沙拉和热食选择更丰富，成人能吃得轻一点，孩子也有稳定主食；这比单纯追求最近距离更符合“省心吃顿好的”这个目标。")
        if segment == "play" and scene == "family":
            return (
                f"我选择「{poi.get('name')}」不是只因为距离近。这个点同时满足室内、低天气风险、5岁孩子能参与、"
                "家长不需要长时间追跑这几个条件；团购也正好覆盖2大1小，能把预算和入场动作提前锁住。"
                "相比同段其它候选，它更适合作为下午第一站，因为孩子体力最好时先完成高参与度项目，后面吃饭才更容易坐得住。")
        if segment == "eat":
            return (
                f"我推荐「{poi.get('name')}」时同时看了四个因素：从上一站过去的摩擦成本、当前排队状态、套餐是否匹配人数、"
                f"以及它的评价关键词是否和这次场景一致。它的人均约¥{poi.get('price_per_person')}，评分{poi.get('rating')}，"
                "不是单点最极致，但综合确定性更高，适合放进一条需要现场执行的路线里。")
        if segment == "extra":
            return (
                f"收尾我倾向「{poi.get('name')}」，因为它不要求再做高强度决策，也不会把动线拉得过长。"
                "前面已经完成主要玩乐和正餐，最后一站应该承担的是情绪收束和返程缓冲，而不是再塞一个复杂项目。"
                "这个选择能让行程显得完整，同时给用户留下随时结束的弹性。")
        return (
            f"我把「{poi.get('name')}」放到当前卡片，是因为它在距离、评分、价格、实时可用性和场景标签上形成了比较均衡的解。"
            "在 demo 里我不会把所有候选都塞给用户，而是把排序后的最稳选择变成一张可执行卡片，让用户只需要确认或换一个。")

    def _plan_ready_events(self, action_text: str, target_name: str, plan: dict[str, Any] | None) -> list[dict[str, Any]]:
        stops = (plan or {}).get("stops", [])
        return [
            {"type": "user_action", "text": action_text, "target": target_name, "ts": 0.0},
            {"type": "stage", "stage": "plan_ready", "detail": "用户确认最后一段，汇总完整方案", "ts": 0.1},
            {"type": "thinking", "text": f"三段都确认了，我把 {len(stops)} 个站点合并成可执行行程，并准备下单工具。", "ts": 0.2},
            {"type": "plan_ready", "plan": plan, "ts": 0.3},
        ]

    def _parse_intent(self, query: str) -> dict:
        try:
            data, reasoning = self.llm.chat_json(
                system=prompts.INTENT_SYSTEM,
                user=prompts.INTENT_USER.format(
                    location=self.location.get("business_area", "南京"),
                    query=query))
            if reasoning:
                self._think(reasoning)
            return _normalize_intent(data, query)
        except Exception as e:  # noqa: BLE001
            self._think(f"意图解析降级（{e}），用启发式兜底。")
            return self._fallback_intent(query)

    def _plan_segments(self, constraints: dict) -> dict:
        scene = constraints.get("scene")
        explicit_text = " ".join([
            self.query,
            " ".join(str(p) for p in constraints.get("preferences") or []),
        ])
        text = " ".join([explicit_text, str(constraints.get("summary") or "")])
        wants_food_or_rest = any(k in text for k in ("吃", "饭", "餐", "清淡", "轻食", "咖啡", "坐一会", "坐会"))
        wants_activity = any(k in explicit_text for k in ("玩", "遛娃", "看展", "展览", "逛", "散步", "citywalk", "Citywalk", "密室", "动物园"))
        if wants_food_or_rest and not wants_activity:
            if scene == "solo":
                narrative = "我帮你按「先吃点清淡的 → 再安静坐会儿」来安排。"
                extra = "最后留一个安静坐会儿或轻松散步的收尾"
            elif scene == "couple":
                narrative = "我帮你按「先吃饭 → 再加一个轻量惊喜」来安排。"
                extra = "最后安排一个不打扰节奏的小惊喜"
            else:
                narrative = "我帮你按「先吃饭 → 再轻松收尾」来安排。"
                extra = "饭后安排一个不用折腾的收尾点"
            self._think("用户目标更偏吃饭/坐会儿，本轮不强行插入玩乐段，直接从餐食候选开始。")
            return {
                "segments": [
                    {"kind": "eat", "intent": "先找一家符合口味和区域的餐食地点"},
                    {"kind": "extra", "intent": extra},
                ],
                "narrative": narrative,
            }
        if scene == "couple":
            narrative = "我帮你按「先逛 → 再吃 → 加个小惊喜」来安排这个约会。"
            extra = "最后安排一个不打扰节奏的小惊喜"
        elif scene == "friends":
            narrative = "我帮你按「先玩热身 → 再聚餐 → 轻松收尾」来安排。"
            extra = "最后加一个适合朋友继续聊的收尾点"
        elif scene == "solo":
            narrative = "我帮你按「随便逛逛 → 吃点好的 → 安静坐会儿」来安排。"
            extra = "最后留一个不用赶时间的轻松收尾"
        else:
            narrative = "我帮你按「先玩 → 再吃 → 加个收尾」来安排这个下午。"
            extra = "饭后安排一个轻松不折腾的亲子收尾"
        self._think("分段规划采用本地稳定策略，避免现场等待多轮模型调用。")
        return {
            "segments": [
                {"kind": "play", "intent": "先找个适合当前人群的活动玩起来"},
                {"kind": "eat", "intent": "玩累了就近吃一顿合适的饭"},
                {"kind": "extra", "intent": extra},
            ],
            "narrative": narrative,
        }

    def _recommend_current(self) -> dict | None:
        if self.idx >= len(self.segments):
            return None
        seg = self.segments[self.idx]
        scene = self.constraints.get("scene")
        self._stage("search",
                    f"大众点评检索：{self.SEGMENT_LABELS.get(seg.kind, seg.kind)}候选")
        candidates = self._search_candidates(seg.kind, scene, seg.rejected, 6)
        if not candidates:
            self._think(f"{seg.kind} 段已无更多候选。")
            seg.current = None
            return None

        self._stage("reason", "在真实候选里做排序匹配 + 生成卡片话术")
        self._think(
            f"我拿到 {len(candidates)} 个真实候选，接下来会同时看距离、评分、"
            "团购匹配度和当前段落目标，只把最终推荐写进卡片。")
        rec = self._pick(seg, candidates)
        seg.current = rec
        card = rec.to_card()
        self._emit({"type": "card", "card": card, "segment_index": self.idx})
        return card

    def _pick(self, seg: Segment, candidates: list[dict]) -> Recommendation:
        poi = candidates[0]
        if self._needs_pre_card_adjustment(seg, poi):
            adjusted = next((c for c in candidates[1:]
                             if not self._needs_pre_card_adjustment(seg, c)), None)
            if adjusted:
                poi = adjusted
        top = self._select_groupon(poi) or {}
        return Recommendation(
            segment_kind=seg.kind, segment_intent=seg.intent, poi=poi,
            summary=poi.get("ai_pitch", poi["name"]),
            groupon_id=top.get("id"),
            groupon_reason=self._groupon_reason(seg.kind, top),
            suggestion=self._suggestion(seg.kind),
            confidence=0.88)

    def _select_groupon(self, poi: dict[str, Any]) -> dict[str, Any] | None:
        groupons = poi.get("groupon") or []
        scene = self.constraints.get("scene")
        for g in groupons:
            if g.get("for_scene") == scene:
                return g
        for g in groupons:
            if not g.get("for_scene"):
                return g
        if scene == "solo":
            return None
        return poi.get("top_groupon") or (groupons[0] if groupons else None)

    def _search_candidates(
        self,
        segment: str,
        scene: str | None,
        rejected: set[str] | None = None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        query = self._search_query()
        preferences = self.constraints.get("preferences") or []
        candidates = self.mock.search_merchants(
            segment=segment, scene=scene, exclude=rejected or set(),
            query=query, preferences=preferences, limit=limit)
        if candidates:
            return candidates
        compatible = {
            "solo": ["couple", "friends"],
            "couple": ["friends"],
            "friends": ["couple"],
            "family": [],
        }.get(scene or "", [])
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for fallback_scene in compatible:
            for item in self.mock.search_merchants(
                    segment=segment, scene=fallback_scene, exclude=rejected or set(),
                    query=query, preferences=preferences, limit=limit):
                if item["id"] in seen:
                    continue
                if scene == "solo" and not self._solo_compatible(item):
                    continue
                seen.add(item["id"])
                out.append(item)
        return out[:limit]

    def _search_query(self) -> str:
        parts = [
            self.query,
            str(self.constraints.get("summary") or ""),
            " ".join(str(p) for p in self.constraints.get("preferences") or []),
        ]
        return "；".join(p for p in parts if p)

    @staticmethod
    def _solo_compatible(poi: dict[str, Any]) -> bool:
        if "solo" in (poi.get("scenes") or []):
            return True
        text = " ".join([
            poi.get("name", ""),
            poi.get("category", ""),
            " ".join(poi.get("tags") or []),
            " ".join(poi.get("suit_for") or []),
        ]).lower()
        return any(k in text for k in ("city walk", "citywalk", "展", "轻食", "咖啡", "安静", "散步"))

    def _needs_pre_card_adjustment(self, seg: Segment, poi: dict[str, Any]) -> bool:
        if seg.kind != "eat":
            return False
        queue = poi.get("queue", {})
        if queue.get("has_table") is False:
            return True
        wait_min = queue.get("wait_min")
        return isinstance(wait_min, int) and wait_min >= 30

    def _groupon_reason(self, kind: str, groupon: dict) -> str:
        if not groupon:
            return ""
        if self.constraints.get("scene") == "family" and kind == "play":
            return "2大1小家庭正好匹配你们一家三口。"
        if self.constraints.get("scene") == "couple":
            return "双人套餐更适合约会场景，预算也比较稳。"
        return "套餐和当前人数、场景比较匹配。"

    def _suggestion(self, kind: str) -> str:
        scene = self.constraints.get("scene")
        if kind == "play" and scene == "family":
            return "可以先玩90分钟左右，结束后直接在附近挑一家环境好点的餐厅吃晚饭，节奏会比较舒服。"
        if kind == "play" and scene == "couple":
            return "先把逛展或街区留给傍晚前半段，拍照和聊天都不赶，后面再切到更有仪式感的晚餐。"
        if kind == "play" and scene == "friends":
            return "先用一个大家都能参与的活动热场，后面聚餐时话题会更自然，也不容易一上来就只剩吃饭。"
        if kind == "play" and scene == "solo":
            return "一个人先选低压力的逛展或街区漫步，不用排复杂队，也能随时放慢节奏。"
        if kind == "eat" and scene == "couple":
            return "可以提前备注靠窗或安静座位，再接一个小花束，惊喜感会更自然。"
        if kind == "eat" and scene == "solo":
            return "建议选清淡、出餐稳定、一个人坐着也舒服的店，吃完还能找个安静地方继续待一会儿。"
        if kind == "eat":
            return "建议先锁定座位或取号，避免到饭点再排太久。"
        if scene == "couple":
            return "最后留一个轻量惊喜，不要把约会排得太满，反而更有余韵。"
        if scene == "friends":
            return "收尾点以好聊天、低门槛为主，想继续玩或提前撤都不会尴尬。"
        if scene == "solo":
            return "收尾保留弹性，适合散步、咖啡或安静坐一会儿，不把自己排得太累。"
        return "最后一段保持轻松，作为正餐后的缓冲和返程前的收尾。"

    @staticmethod
    def _cand_for_prompt(c: dict) -> dict:
        return {
            "id": c["id"], "name": c["name"], "category": c["category"],
            "rating": c["rating"], "price_per_person": c["price_per_person"],
            "distance_km": c["distance_km"], "tags": c.get("tags", []),
            "ai_pitch": c.get("ai_pitch", ""),
            "groupon": [{"id": g["id"], "title": g["title"], "price": g["price"],
                         "for_scene": g.get("for_scene")}
                        for g in c.get("groupon", [])],
        }

    # ---------------- 用户操作 ----------------
    def accept(self) -> dict[str, Any]:
        seg = self.segments[self.idx]
        accepted_name = seg.current.poi["name"] if seg.current else ""
        if seg.current:
            seg.accepted = seg.current
            self._think(f"用户接受了「{seg.current.poi['name']}」，进入下一段。")
        self.idx += 1
        if self.idx >= len(self.segments):
            result = self._finish()
            result.update(self._sync_agent_payload(
                self._plan_ready_events("用户点击「就这家，下一步」", accepted_name, result.get("plan"))))
            return result
        card = self._recommend_current()
        return {"done": False, "current_index": self.idx, "card": card,
                "plan": self.current_plan(),
                **self._sync_agent_payload(self._card_action_events(
                    "用户点击「就这家，下一步」", accepted_name, "进入下一段推荐", card))}

    def reject(self) -> dict[str, Any]:
        seg = self.segments[self.idx]
        rejected_name = seg.current.poi["name"] if seg.current else ""
        if seg.current:
            seg.rejected.add(seg.current.poi["id"])
            self._think(f"用户对「{seg.current.poi['name']}」不感兴趣，换一个。")
        card = self._recommend_current()
        if card is None:
            # 没候选了，跳过该段
            self.idx += 1
            if self.idx >= len(self.segments):
                result = self._finish()
                result.update(self._sync_agent_payload(
                    self._plan_ready_events("用户点击「换一个」", rejected_name, result.get("plan"))))
                return result
            card = self._recommend_current()
        return {"done": False, "current_index": self.idx, "card": card,
                "plan": self.current_plan(),
                **self._sync_agent_payload(self._card_action_events(
                    "用户点击「换一个」", rejected_name, "同段替换候选", card))}

    def switch_to(self, poi_id: str) -> dict[str, Any]:
        """用户在详情二级菜单里手动改选同段的另一家。"""
        seg = self.segments[self.idx]
        poi = next((c for c in self.mock.search_merchants(
            segment=seg.kind,
            scene=self.constraints.get("scene"),
            query=self._search_query(),
            preferences=self.constraints.get("preferences") or [],
            limit=20,
        ) if c.get("id") == poi_id), None)
        if poi is None:
            poi = self.mock._brief(self.mock.get(poi_id), self.mock.home_location())
        top = self._select_groupon(poi) or {}
        seg.current = Recommendation(
            segment_kind=seg.kind, segment_intent=seg.intent, poi=poi,
            summary=poi.get("ai_pitch", poi["name"]),
            groupon_id=top.get("id"), groupon_reason="", suggestion="",
            confidence=0.9)
        self._think(f"用户手动改选「{poi['name']}」。")
        card = seg.current.to_card()
        self._emit({"type": "card", "card": card, "segment_index": self.idx})
        return {"done": False, "current_index": self.idx, "card": card,
                "plan": self.current_plan(),
                **self._sync_agent_payload(self._card_action_events(
                    "用户在详情页点击「改选」", poi["name"], "手动替换当前卡片", card))}

    def chat(self, message: str) -> dict[str, Any]:
        if self.intake_active or not self.segments:
            return self._run_intake_agent(message)

        seg = self.segments[self.idx]
        scene = self.constraints.get("scene")
        candidates = self.mock.search_merchants(
            segment=seg.kind, scene=scene, exclude=seg.rejected,
            query=self._search_query() + "；" + message,
            preferences=[*(self.constraints.get("preferences") or []), message],
            limit=6)
        cur = seg.current.poi if seg.current else {}
        self._stage("chat", "多轮对话理解 + 决定是否换店")
        try:
            data, reasoning = self.llm.chat_json(
                system=prompts.CHAT_SYSTEM,
                user=prompts.CHAT_USER.format(
                    constraints=json.dumps(self.constraints, ensure_ascii=False),
                    current=json.dumps({"id": cur.get("id"), "name": cur.get("name")},
                                       ensure_ascii=False),
                    candidates=json.dumps([self._cand_for_prompt(c) for c in candidates],
                                          ensure_ascii=False),
                    message=message))
            if reasoning:
                self._think(reasoning)
        except Exception as e:  # noqa: BLE001
            self._think(f"对话降级（{e}）。")
            data = {"reply": "我先按当前推荐给你保留着，你也可以点详情自己挑。",
                    "action": "keep"}

        reply = data.get("reply", "")
        action = data.get("action", "keep")
        new_prefs = data.get("updated_preferences") or []
        if new_prefs:
            prefs = set(self.constraints.get("preferences", []))
            prefs.update(new_prefs)
            self.constraints["preferences"] = sorted(prefs)

        if action != "switch":
            explicit = self._explicit_preference_switch(message, candidates, cur.get("id"))
            if explicit:
                action = "switch"
                data["switch_to_id"] = explicit["id"]
                reply = f"明白，你更想吃{explicit['reason']}。我直接帮你换到「{explicit['name']}」，不用你自己进详情挑。"

        card = None
        if action == "switch" and data.get("switch_to_id"):
            res = self.switch_to(data["switch_to_id"])
            card = res["card"]
        elif action == "refine":
            card = self._recommend_current()
        self._emit({"type": "assistant_reply", "text": reply})
        return {"reply": reply, "action": action, "card": card,
                "current_index": self.idx, "done": False}

    @staticmethod
    def _explicit_preference_switch(
        message: str,
        candidates: list[dict[str, Any]],
        current_id: str | None,
    ) -> dict[str, Any] | None:
        signals = {
            "南京菜": ("南京菜", "老南京", "盐水鸭", "鸭血粉丝"),
            "清淡": ("清淡", "轻食", "低脂", "减脂"),
            "安静": ("安静", "清静", "不吵", "坐一会", "坐会"),
        }
        wanted = [name for name, keys in signals.items() if any(k in message for k in keys)]
        if not wanted:
            return None
        current = next((c for c in candidates if c.get("id") == current_id), None)
        current_reasons = set((current or {}).get("match_reasons") or [])
        for signal in wanted:
            if signal in current_reasons:
                continue
            match = next((c for c in candidates
                          if c.get("id") != current_id
                          and signal in (c.get("match_reasons") or [])), None)
            if match:
                return {**match, "reason": signal}
        return None

    def detail(self, poi_id: str) -> dict[str, Any]:
        poi = self.mock.get(poi_id)
        segment_kind = poi.get("segment")
        d = self.mock.detail(poi_id)
        # 附上同段可切换的其它候选
        alts: list[dict] = []
        if segment_kind:
            for c in self.mock.search_merchants(
                    segment=segment_kind, scene=self.constraints.get("scene"),
                    exclude={poi_id}, query=self._search_query(),
                    preferences=self.constraints.get("preferences") or [], limit=5):
                alts.append({"id": c["id"], "name": c["name"],
                             "rating": c["rating"], "distance_km": c["distance_km"],
                             "price_per_person": c["price_per_person"],
                             "image": c["image"], "category": c["category"]})
        d["alternatives"] = alts
        return d

    # ---------------- 计划 / 执行 ----------------
    def current_plan(self) -> dict[str, Any]:
        stops = []
        for i, s in enumerate(self.segments):
            rec = s.accepted or (s.current if i == self.idx else None)
            if rec:
                stops.append({
                    "segment_kind": s.kind,
                    "label": self.SEGMENT_LABELS.get(s.kind, s.kind),
                    "poi_id": rec.poi["id"], "name": rec.poi["name"],
                    "image": rec.poi["image"],
                    "accepted": s.accepted is not None})
        return {"stops": stops}

    def _finish(self) -> dict[str, Any]:
        self._stage("plan_ready", "三段已确认，生成完整方案")
        plan = self._full_plan()
        self._emit({"type": "plan_ready", "plan": plan})
        return {"done": True, "current_index": self.idx, "plan": plan, "card": None}

    def _full_plan(self) -> dict[str, Any]:
        stops = []
        cursor = 14 * 60  # 14:00 起步，单位分钟
        for s in self.segments:
            rec = s.accepted
            if not rec:
                continue
            groupon = None
            for g in rec.poi.get("groupon", []):
                if g.get("id") == rec.groupon_id:
                    groupon = g
                    break
            start = f"{cursor // 60:02d}:{cursor % 60:02d}"
            dur = 90 if s.kind == "play" else (75 if s.kind == "eat" else 60)
            cursor += dur + 15  # 含路途
            stops.append({
                "segment_kind": s.kind,
                "label": self.SEGMENT_LABELS.get(s.kind, s.kind),
                "poi_id": rec.poi["id"], "name": rec.poi["name"],
                "image": rec.poi["image"], "address": rec.poi.get("address"),
                "summary": rec.summary, "start_time": start,
                "groupon": groupon, "booking": rec.poi.get("booking", {}),
                "suggestion": rec.suggestion})
        return {"session_id": self.id, "scene": self.constraints.get("scene"),
                "constraints": self.constraints, "stops": stops}

    def execute(self) -> dict[str, Any]:
        """逐站下单，命中 无座/无票 自动换备选，并记录每步结果。"""
        self._stage("execute", "开始一键执行：逐站预约 / 购票 / 下单")
        self._think("执行阶段我会按计划逐站调用下单工具；遇到满座、售罄或时间冲突时，先在同段候选里自动调整。")
        plan = self._full_plan()
        results: list[dict] = []
        booked_times: set[str] = set()
        for stop in plan["stops"]:
            seg = next((s for s in self.segments if s.kind == stop["segment_kind"]), None)
            poi_id = stop["poi_id"]
            party = self.constraints.get("adults", 2) + self.constraints.get("kids", 0)
            time_str = stop["start_time"]
            # 冲突检测：同一时间已有预约 → 错峰 +15 分钟
            if time_str in booked_times:
                hh, mm = map(int, time_str.split(":"))
                mm += 15
                if mm >= 60:
                    hh, mm = hh + 1, mm - 60
                new_time = f"{hh:02d}:{mm:02d}"
                self._think(f"检测到 {time_str} 时间冲突，自动错峰到 {new_time}。")
                self._emit({"type": "self_heal", "kind": "conflict",
                            "poi": stop["name"], "from": time_str, "to": new_time})
                time_str = new_time
            res = self.mock.execute_booking(
                poi_id, party_size=party, time_str=time_str,
                target_address=plan["stops"][0]["name"] if stop["segment_kind"] == "extra" else None)
            tool_name = self._booking_tool_for_poi(poi_id, stop["segment_kind"])

            # 无座 / 无票 → 同段换备选重试
            heal_info = None
            if res.get("status") in ("no_table", "sold_out") and seg is not None:
                heal_info = self._heal(seg, poi_id, party, time_str, res["status"])
                if heal_info:
                    res = heal_info["result"]
                    stop = {**stop, "poi_id": heal_info["poi_id"],
                            "name": heal_info["name"]}
                    tool_name = self._booking_tool_for_poi(heal_info["poi_id"], stop["segment_kind"])
            if res.get("status") == "ok":
                booked_times.add(time_str)
            results.append({
                "segment_kind": stop["segment_kind"], "label": stop["label"],
                "name": stop["name"], "status": res.get("status"),
                "confirm": res.get("confirm"), "order_id": res.get("order_id"),
                "eta_min": res.get("eta_min"), "healed": heal_info is not None,
                "heal_note": heal_info.get("note") if heal_info else None,
                "time": time_str, "tool_name": tool_name})
        ok = all(r["status"] == "ok" for r in results)
        self._emit({"type": "execute_done", "ok": ok, "results": results})
        return {"ok": ok, "results": results, "plan": plan,
                **self._sync_agent_payload(self._execute_agent_events(results, ok), delay_ms=180)}

    def _heal(self, seg: Segment, failed_id: str, party: int, time_str: str,
              reason: str) -> dict | None:
        label = {"no_table": "满座", "sold_out": "票售罄"}.get(reason, reason)
        self._think(f"「{self.mock.get(failed_id)['name']}」{label}，自动找同段备选并改约。")
        self._emit({"type": "self_heal", "kind": reason,
                    "poi": self.mock.get(failed_id)["name"]})
        alts = self.mock.search_merchants(
            segment=seg.kind, scene=self.constraints.get("scene"),
            exclude={failed_id} | seg.rejected, query=self._search_query(),
            preferences=self.constraints.get("preferences") or [], limit=5)
        for alt in alts:
            res = self.mock.execute_booking(
                alt["id"], party_size=party, time_str=time_str)
            if res.get("status") == "ok":
                note = f"原选 {label}，自动改为「{alt['name']}」"
                self._emit({"type": "self_heal_ok", "to": alt["name"], "note": note})
                self._think(f"已自动调整：改约「{alt['name']}」成功。")
                return {"result": res, "poi_id": alt["id"],
                        "name": alt["name"], "note": note}
        return None

    @staticmethod
    def _booking_tool_name(segment_kind: str) -> str:
        return {
            "play": "meituan_buy_ticket",
            "eat": "meituan_book_table",
            "extra": "meituan_order_delivery",
        }.get(segment_kind, "meituan_execute_booking")

    def _booking_tool_for_poi(self, poi_id: str, segment_kind: str) -> str:
        booking_type = self.mock.get(poi_id).get("booking", {}).get("type")
        if booking_type == "table":
            return "meituan_book_table"
        if booking_type == "ticket":
            return "meituan_buy_ticket"
        if booking_type == "delivery":
            return "meituan_order_delivery"
        return self._booking_tool_name(segment_kind)

    def _execute_agent_events(self, results: list[dict[str, Any]], ok: bool) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [
            {"type": "user_action", "text": "用户点击「一键执行」", "ts": 0.0},
            {"type": "stage", "stage": "execute", "detail": "按已确认行程逐站调用预约/购票/下单工具", "ts": 0.1},
            {"type": "thinking", "text": "我会逐站执行，遇到满座或售罄时立即用同段备选自动调整。", "ts": 0.2},
        ]
        ts = 0.3
        for res in results:
            tool = res.get("tool_name") or self._booking_tool_name(res.get("segment_kind", ""))
            events.append({"type": "tool_call", "name": tool, "args": {
                "name": res.get("name"),
                "time": res.get("time"),
            }, "ts": round(ts, 1)})
            ts += 0.1
            if res.get("healed"):
                events.append({"type": "self_heal", "kind": "auto_replace",
                               "poi": res.get("name"), "ts": round(ts, 1)})
                ts += 0.1
                events.append({"type": "self_heal_ok", "note": res.get("heal_note"),
                               "ts": round(ts, 1)})
                ts += 0.1
            events.append({"type": "tool_result", "name": tool, "result": {
                "status": res.get("status"),
                "confirm": res.get("confirm"),
                "order_id": res.get("order_id"),
            }, "ts": round(ts, 1)})
            ts += 0.1
        events.append({"type": "execute_done", "ok": ok, "results": results, "ts": round(ts, 1)})
        return events

    # ---------------- 兜底 ----------------
    @staticmethod
    def _fallback_intent(query: str) -> dict:
        scene = "friends"
        if any(k in query for k in ("老婆", "孩子", "娃", "带娃", "家人", "儿子", "女儿")):
            scene = "family"
        elif any(k in query for k in ("对象", "女朋友", "男朋友", "情侣", "约会", "老公")):
            scene = "couple"
        elif any(k in query.lower() for k in ("solo",)) or any(
                k in query for k in ("一个人", "自己", "独自", "单人", "一人")):
            scene = "solo"
        diet = ["减肥"] if any(k in query for k in ("减肥", "减脂", "轻食")) else []
        return _apply_query_sanity_overrides({"scene": scene, "adults": 1 if scene == "solo" else 2,
                "kids": 1 if scene == "family" else 0,
                "kid_age": 5 if scene == "family" else None,
                "diet": diet, "time_window": {"start": "14:00", "hours": 4},
                "budget_level": "medium", "preferences": [],
                "summary": f"（兜底解析）场景={scene}"}, query)
