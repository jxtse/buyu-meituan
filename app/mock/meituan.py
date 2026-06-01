"""美团 / 大众点评 Mock API 层。

全程不接真实接口，模拟以下能力：
- 美团地图 / 定位：locate() 返回用户当前坐标 + 商圈名
- 点评商户检索：search_merchants(segment, scene, ...) 按段/场景/距离召回
- 点评种草帖：dianping_notes(poi_id) 返回带图文案
- 美团团购：groupons(poi_id) / 精选评论 reviews(poi_id)
- 美团预约 & 下单：book_table / buy_ticket / order_delivery —— 带「无座/无票/冲突」故障注入
- 计划分享：share_plan()

每个写操作都会通过 event_bus 推一条 tool_call / tool_result 事件，
供右侧技术面板实时展示「工具调用链路」。
"""
from __future__ import annotations

import copy
import json
import math
import random
import time
import uuid
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).parent / "data" / "merchants.json"

# 故障注入开关：现场 demo 触发「无座 / 无票 / 冲突」自动调整用
_FAULTS: set[str] = set()
# 已知故障种子：这些商户天然触发对应故障（无需手动开开关）
FAULT_SEEDS = {
    "no_table": {"green_bowl_hexi", "hexi_hotpot"},
    "sold_out": {"aqua_kids_zero", "museum_future_zero"},
}


def enable_fault(kind: str) -> None:
    _FAULTS.add(kind)


def disable_faults() -> None:
    _FAULTS.clear()


def active_faults() -> set[str]:
    return set(_FAULTS)


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lng1, lat1 = a
    lng2, lat2 = b
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * radius * math.asin(min(1.0, math.sqrt(h))), 2)


class MeituanMock:
    """美团/点评 Mock 数据中枢。线程内单例即可（数据只读 + 少量内存状态）。"""

    def __init__(self, data_path: Path | str | None = None, *, bus: Any = None) -> None:
        path = Path(data_path) if data_path else DATA_PATH
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self._items: list[dict[str, Any]] = data
        self._by_id = {str(m["id"]): m for m in data}
        self.bus = bus
        self._orders: dict[str, dict] = {}

    # ---------------- 地图 / 定位 ----------------
    def locate(self) -> dict[str, Any]:
        """模拟美团地图定位：返回用户坐标 + 商圈。"""
        self._emit_call("meituan_locate", {})
        result = {
            "lng": 118.7372, "lat": 32.0148,
            "district": "南京市建邺区",
            "business_area": "金陵天地 · 河西万达商圈",
            "accuracy_m": 35,
        }
        self._emit_result("meituan_locate", result)
        return result

    def home_location(self) -> tuple[float, float]:
        return (118.7372, 32.0148)

    # ---------------- 商户检索 ----------------
    def search_merchants(
        self, *, segment: str, scene: str | None = None,
        exclude: set[str] | None = None, near: tuple[float, float] | None = None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """按 段(play/eat/extra) + 场景(family/couple/friends) 召回候选，按距离排序。"""
        self._emit_call("dianping_search", {
            "segment": segment, "scene": scene, "exclude": sorted(exclude or [])})
        exclude = exclude or set()
        center = near or self.home_location()
        out: list[dict[str, Any]] = []
        for m in self._items:
            if m.get("segment") != segment:
                continue
            if m["id"] in exclude:
                continue
            if scene and scene not in (m.get("scenes") or []):
                continue
            brief = self._brief(m, center)
            out.append(brief)
        # 距离近 + 评分高优先
        out.sort(key=lambda x: (x["distance_km"], -x["rating"]))
        result = out[:limit]
        self._emit_result("dianping_search", {"count": len(result),
                                              "ids": [r["id"] for r in result]})
        return result

    def get(self, poi_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._by_id[str(poi_id)])

    def detail(self, poi_id: str) -> dict[str, Any]:
        """商户详情页：完整信息 + 种草帖 + 精选评论 + 团购 + 图集。"""
        self._emit_call("dianping_detail", {"poi_id": poi_id})
        m = self.get(poi_id)
        center = self.home_location()
        dist = _distance_km(center, tuple(m["location"]))
        result = {
            "id": m["id"], "name": m["name"], "category": m["category"],
            "address": m["address"], "rating": m["rating"],
            "review_count": m["review_count"], "price_per_person": m["price_per_person"],
            "tags": m.get("tags", []), "open_hours": m.get("open_hours"),
            "distance_km": dist, "image": self._image_ref(m),
            "gallery": [self._image_ref(m)],
            "ai_pitch": m.get("ai_pitch", ""),
            "groupon": m.get("groupon", []),
            "booking": m.get("booking", {}),
            "queue": m.get("queue", {}),
            "dianping_notes": m.get("dianping_notes", []),
            "reviews": sorted(m.get("reviews", []),
                              key=lambda r: -r.get("useful", 0)),
        }
        self._emit_result("dianping_detail", {"poi_id": poi_id,
                                              "notes": len(result["dianping_notes"])})
        return result

    def dianping_notes(self, poi_id: str) -> list[dict]:
        return self.get(poi_id).get("dianping_notes", [])

    # ---------------- 预约 / 下单（带故障注入）----------------
    def _fault_active(self, kind: str, poi_id: str) -> bool:
        if kind in _FAULTS:
            return True
        return poi_id in FAULT_SEEDS.get(kind, set())

    def book_table(self, *, poi_id: str, party_size: int, time_str: str) -> dict:
        self._emit_call("meituan_book_table",
                        {"poi_id": poi_id, "party_size": party_size, "time": time_str})
        m = self.get(poi_id)
        if self._fault_active("no_table", poi_id) or \
                m.get("queue", {}).get("has_table") is False:
            res = {"status": "no_table", "poi_id": poi_id, "name": m["name"],
                   "wait_min": m.get("queue", {}).get("wait_min", 30)}
        else:
            res = {"status": "ok", "poi_id": poi_id, "name": m["name"],
                   "order_id": self._mk_order("table", poi_id),
                   "party_size": party_size, "time": time_str,
                   "confirm": f"已取号：{m['name']} {time_str} {party_size} 人位"}
        self._emit_result("meituan_book_table", res)
        return res

    def buy_ticket(self, *, poi_id: str, count: int) -> dict:
        self._emit_call("meituan_buy_ticket", {"poi_id": poi_id, "count": count})
        m = self.get(poi_id)
        left = m.get("tickets_left")
        if self._fault_active("sold_out", poi_id) or (left is not None and left <= 0):
            res = {"status": "sold_out", "poi_id": poi_id, "name": m["name"]}
        else:
            res = {"status": "ok", "poi_id": poi_id, "name": m["name"],
                   "order_id": self._mk_order("ticket", poi_id), "count": count,
                   "confirm": f"已为你购买 {m['name']} {count} 张票"}
        self._emit_result("meituan_buy_ticket", res)
        return res

    def order_delivery(self, *, poi_id: str, item: str, target_address: str) -> dict:
        self._emit_call("meituan_order_delivery",
                        {"poi_id": poi_id, "item": item, "to": target_address})
        m = self.get(poi_id)
        res = {"status": "ok", "poi_id": poi_id, "name": m["name"],
               "order_id": self._mk_order("delivery", poi_id), "item": item,
               "eta_min": random.randint(25, 40), "to": target_address,
               "confirm": f"已下单「{item}」，预计 30 分钟内送达 {target_address}"}
        self._emit_result("meituan_order_delivery", res)
        return res

    def execute_booking(self, poi_id: str, *, party_size: int, time_str: str,
                        target_address: str | None = None) -> dict:
        """按商户的 booking.type 自动选择 预约/购票/外卖 动作。"""
        booking = self.get(poi_id).get("booking", {})
        btype = booking.get("type")
        if btype == "table":
            return self.book_table(poi_id=poi_id, party_size=party_size, time_str=time_str)
        if btype == "ticket":
            return self.buy_ticket(poi_id=poi_id, count=party_size)
        if btype == "delivery":
            return self.order_delivery(
                poi_id=poi_id, item=booking.get("label", "外卖"),
                target_address=target_address or "指定餐厅")
        # type == none：免预约
        m = self.get(poi_id)
        res = {"status": "ok", "poi_id": poi_id, "name": m["name"],
               "order_id": None, "confirm": f"{m['name']} 免预约，到场即可"}
        self._emit_result("meituan_visit", res)
        return res

    def share_plan(self, plan: dict) -> dict:
        self._emit_call("meituan_share_plan", {"segments": len(plan.get("stops", []))})
        share_id = uuid.uuid4().hex[:8]
        res = {"status": "ok", "share_id": share_id,
               "url": f"/share/{share_id}", "plan": plan}
        self._emit_result("meituan_share_plan", {"share_id": share_id})
        return res

    # ---------------- 内部 ----------------
    def _mk_order(self, kind: str, poi_id: str) -> str:
        oid = f"MT{kind[:1].upper()}{uuid.uuid4().hex[:8]}"
        self._orders[oid] = {"kind": kind, "poi_id": poi_id, "ts": time.time()}
        return oid

    def _brief(self, m: dict, center: tuple[float, float]) -> dict:
        dist = _distance_km(center, tuple(m["location"]))
        top_groupon = (m.get("groupon") or [None])[0]
        return {
            "id": m["id"], "name": m["name"], "category": m["category"],
            "segment": m["segment"], "rating": m["rating"],
            "review_count": m["review_count"], "price_per_person": m["price_per_person"],
            "tags": m.get("tags", []), "distance_km": dist, "image": self._image_ref(m),
            "scenes": m.get("scenes", []), "suit_for": m.get("suit_for", []),
            "ai_pitch": m.get("ai_pitch", ""), "address": m["address"],
            "queue": m.get("queue", {}), "booking": m.get("booking", {}),
            "tickets_left": m.get("tickets_left"),
            "top_groupon": top_groupon,
            "groupon": m.get("groupon", []),
            "open_hours": m.get("open_hours"),
        }

    @staticmethod
    def _image_ref(m: dict) -> str:
        image = str(m.get("image") or "").strip()
        if image:
            if image.startswith(("/", "http://", "https://")):
                return image
            return f"/static/{image}"
        return f"/api/place-image/{m['id']}?v=20260601-photo1"

    def _emit_call(self, name: str, args: dict) -> None:
        self._publish({"type": "tool_call", "source": "meituan_mock",
                       "name": name, "args": args})

    def _emit_result(self, name: str, result: dict) -> None:
        self._publish({"type": "tool_result", "name": name,
                       "result": _truncate(result)})

    def _publish(self, ev: dict) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(ev)
        except Exception:
            pass


def _truncate(obj: Any, limit: int = 400) -> Any:
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= limit:
        return obj
    return {"_summary": s[:limit] + "…"}
