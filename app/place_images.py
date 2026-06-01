"""Resolve presentable venue imagery for the demo.

The local mock corpus keeps stable POI IDs and coordinates, while the actual
visual asset can come from exact Amap POI photos or broader real venue photos.
"""
from __future__ import annotations

from dataclasses import dataclass
import mimetypes
from pathlib import Path
from typing import Any

import httpx

AMAP_PLACE_TEXT_URL = "https://restapi.amap.com/v5/place/text"
BASE = Path(__file__).parent

DEFAULT_LOCAL_FALLBACKS = {
    "kiddo_lab": "gen_kiddo_lab.png",
    "xuanwu_bounce": "gen_xuanwu_bounce.png",
    "aqua_kids_zero": "gen_aqua_kids.png",
    "deji_light_show": "gen_deji_light_show.png",
    "museum_future_zero": "gen_museum_future.png",
    "fuzimiao_snack_walk": "gen_fuzimiao_snack_walk.png",
    "green_bowl_hexi": "gen_green_bowl.png",
    "sprout_table": "gen_sprout_table.png",
    "hexi_hotpot": "gen_hexi_hotpot.png",
    "roundtable_bbq": "gen_roundtable_bbq.png",
    "qinhuai_tapas": "gen_qinhuai_tapas.png",
    "lake_fit_cafe": "gen_lake_fit_cafe.png",
    "qinhuai_bouquet": "gen_qinhuai_bouquet.png",
    "bloom_cake_hexi": "gen_bloom_cake.png",
    "sweet_flower_xjk": "gen_sweet_flower_xjk.png",
    "xuanwu_lake_walk": "gen_xuanwu_lake_walk.png",
    "laomendong_walk": "gen_laomendong_walk.png",
    "qinhuai_folk_art": "gen_qinhuai_folk_art.png",
}


@dataclass(frozen=True)
class ResolvedImage:
    kind: str
    url: str | None = None
    content: bytes = b""
    media_type: str = "image/png"


class PlaceImageResolver:
    def __init__(self, *, amap_key: str | None,
                 client: httpx.Client | None = None,
                 local_assets_dir: str | Path | None = None,
                 local_fallbacks: dict[str, str] | None = None) -> None:
        self.amap_key = amap_key or ""
        self.client = client or httpx.Client(timeout=5.0, trust_env=False)
        self.local_assets_dir = Path(local_assets_dir) if local_assets_dir else BASE / "static" / "place_photos"
        self.local_fallbacks = dict(DEFAULT_LOCAL_FALLBACKS if local_fallbacks is None else local_fallbacks)

    def resolve(self, poi: dict[str, Any]) -> ResolvedImage:
        if not self.amap_key:
            local = self._find_local_photo(poi)
            if local:
                return local
            return ResolvedImage(kind="empty")

        photo_url = self._find_photo_url(poi)
        if photo_url:
            return ResolvedImage(kind="redirect", url=photo_url)

        category_url = self._find_category_photo_url(poi)
        if category_url:
            return ResolvedImage(kind="redirect", url=category_url)

        local = self._find_local_photo(poi)
        if local:
            return local

        return ResolvedImage(kind="empty")

    def _find_local_photo(self, poi: dict[str, Any]) -> ResolvedImage | None:
        filename = self.local_fallbacks.get(str(poi.get("id", "")))
        if not filename:
            return None
        path = self.local_assets_dir / filename
        try:
            content = path.read_bytes()
        except OSError:
            return None
        media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return ResolvedImage(kind="bytes", content=content, media_type=media_type)

    def _find_photo_url(self, poi: dict[str, Any]) -> str | None:
        response = self.client.get(AMAP_PLACE_TEXT_URL, params={
            "key": self.amap_key,
            "keywords": str(poi.get("name", ""))[:80],
            "region": "南京市",
            "city_limit": "true",
            "show_fields": "photos",
            "page_size": 1,
            "page_num": 1,
            "output": "json",
        })
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "1":
            return None
        pois = data.get("pois") or []
        if not pois:
            return None
        for candidate in pois:
            if not self._is_same_place(poi, candidate):
                continue
            photos = candidate.get("photos") or []
            for photo in photos:
                url = photo.get("url") if isinstance(photo, dict) else None
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
        return None

    def _find_category_photo_url(self, poi: dict[str, Any]) -> str | None:
        category = str(poi.get("category") or "").strip()
        if not category:
            return None
        response = self.client.get(AMAP_PLACE_TEXT_URL, params={
            "key": self.amap_key,
            "keywords": f"南京 {category}"[:80],
            "region": "南京市",
            "city_limit": "true",
            "show_fields": "photos",
            "page_size": 5,
            "page_num": 1,
            "output": "json",
        })
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "1":
            return None
        for candidate in data.get("pois") or []:
            for photo in candidate.get("photos") or []:
                url = photo.get("url") if isinstance(photo, dict) else None
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
        return None

    @staticmethod
    def _is_same_place(poi: dict[str, Any], candidate: dict[str, Any]) -> bool:
        target_name = _compact(str(poi.get("name", "")))
        candidate_name = _compact(str(candidate.get("name", "")))
        if target_name and candidate_name and (
                target_name in candidate_name or candidate_name in target_name):
            return True

        target_addr = _compact(str(poi.get("address", "")))
        candidate_addr = _compact(str(candidate.get("address", "")))
        if len(target_addr) >= 8 and len(candidate_addr) >= 8 and (
                target_addr in candidate_addr or candidate_addr in target_addr):
            return True

        tokens = [token for token in _tokens(target_name) if len(token) >= 3]
        return bool(tokens and any(token in candidate_name for token in tokens))

def _compact(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and ch.isalnum():
            buf += ch.lower()
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            if "\u4e00" <= ch <= "\u9fff":
                tokens.append(ch)
    if buf:
        tokens.append(buf)
    return tokens
