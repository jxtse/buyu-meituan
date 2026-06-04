"""本地引力 FastAPI 服务端。

路由：
  GET  /                      前端单页
  GET  /static/*              静态资源（商户图）
  GET  /api/events            SSE：thinking / stage / tool / card / self_heal …
  POST /api/start             {query} 启动会话，返回首段卡片
  POST /api/accept            接受当前卡片，推进下一段
  POST /api/reject            拒绝当前卡片，同段换一个
  POST /api/switch            {poi_id} 用户在详情里手动改选
  POST /api/chat              {message} 多轮对话
  GET  /api/detail/{poi_id}   商户详情（种草帖/团购/评论/可切换候选）
  POST /api/execute           一键执行（含无座/无票/冲突自动调整）
  GET  /api/plan              当前已选计划

浏览器随请求携带可恢复会话 state；同实例内存缓存只做快速回退。SSE 用 EventBus 广播。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .event_bus import EventBus
from .llm import LLMClient
from .mock import meituan as meituan_mod
from .mock.meituan import MeituanMock
from .place_images import PlaceImageResolver
from .session import Session

BASE = Path(__file__).parent
app = FastAPI(title="本地引力 · 周末闲时活动规划 Agent")

# 允许个人站入口页（jxtse.github.io）跨域探活 /api/health。
# Demo 无敏感数据、无鉴权，放开 CORS 不引入风险。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 全局单例 ----------------
_cfg = load_config()
_bus = EventBus()
_llm = LLMClient(
    base_url=_cfg.base_url,
    api_key=_cfg.api_key,
    model=_cfg.model,
    timeout=_cfg.llm_timeout_seconds,
)
_mock = MeituanMock(bus=_bus)
_images = PlaceImageResolver(amap_key=_cfg.amap_key)
_session: Session | None = None
_sessions: dict[str, Session] = {}
_session_lock = asyncio.Lock()


@app.on_event("startup")
async def _startup() -> None:
    _bus.bind_loop(asyncio.get_running_loop())


def _new_session() -> Session:
    global _session
    meituan_mod.disable_faults()
    _session = Session(llm=_llm, mock=_mock, bus=_bus)
    _sessions[_session.id] = _session
    return _session


def _require() -> Session:
    if _session is None:
        raise RuntimeError("no active session; POST /api/start first")
    return _session


def _resolve_session(payload: dict | None = None) -> Session:
    global _session
    data = payload or {}
    state = data.get("state")
    if isinstance(state, dict):
        _session = Session.from_state(state, llm=_llm, mock=_mock, bus=_bus)
        _sessions[_session.id] = _session
        return _session

    session_id = data.get("session_id")
    if session_id and session_id in _sessions:
        _session = _sessions[session_id]
        return _session
    return _require()


async def _run_blocking(fn, *args, **kwargs) -> Any:
    """把会阻塞（LLM 调用）的会话方法丢到线程池，避免堵住事件循环。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------- 页面 ----------------
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")


# ---------------- SSE ----------------
@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    q = _bus.subscribe()

    async def gen():
        # 起手打个招呼，确认通道连通
        yield _sse({"type": "connected"})
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield _sse(ev)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------------- 业务路由 ----------------
@app.post("/api/start")
async def start(payload: dict) -> JSONResponse:
    query = (payload or {}).get("query", "").strip()
    if not query:
        return JSONResponse({"error": "empty query"}, status_code=400)
    source = (payload or {}).get("source", "custom")
    preset_id = (payload or {}).get("preset_id")
    async with _session_lock:
        sess = _new_session()
        result = await _run_blocking(sess.start, query, source=source, preset_id=preset_id)
    return JSONResponse(result)


@app.post("/api/accept")
async def accept(payload: dict | None = None) -> JSONResponse:
    async with _session_lock:
        sess = _resolve_session(payload)
        result = await _run_blocking(sess.accept)
        _sessions[sess.id] = sess
    return JSONResponse(result)


@app.post("/api/reject")
async def reject(payload: dict | None = None) -> JSONResponse:
    async with _session_lock:
        sess = _resolve_session(payload)
        result = await _run_blocking(sess.reject)
        _sessions[sess.id] = sess
    return JSONResponse(result)


@app.post("/api/switch")
async def switch(payload: dict) -> JSONResponse:
    poi_id = (payload or {}).get("poi_id")
    if not poi_id:
        return JSONResponse({"error": "missing poi_id"}, status_code=400)
    async with _session_lock:
        sess = _resolve_session(payload)
        result = await _run_blocking(sess.switch_to, poi_id)
        _sessions[sess.id] = sess
    return JSONResponse(result)


@app.post("/api/chat")
async def chat(payload: dict) -> JSONResponse:
    message = (payload or {}).get("message", "").strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    async with _session_lock:
        sess = _resolve_session(payload)
        result = await _run_blocking(sess.chat, message)
        _sessions[sess.id] = sess
    return JSONResponse(result)


@app.get("/api/detail/{poi_id}")
async def detail(poi_id: str) -> JSONResponse:
    sess = _require()
    result = await _run_blocking(sess.detail, poi_id)
    return JSONResponse(result)


@app.post("/api/execute")
async def execute(payload: dict | None = None) -> JSONResponse:
    async with _session_lock:
        sess = _resolve_session(payload)
        result = await _run_blocking(sess.execute)
        _sessions[sess.id] = sess
    return JSONResponse(result)


@app.get("/api/plan")
async def plan() -> JSONResponse:
    sess = _require()
    return JSONResponse(sess.current_plan())


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "model": _cfg.model,
            "merchants": len(_mock._items),
            "has_session": _session is not None,
            "has_amap_key": bool(_cfg.amap_key)}


@app.get("/api/place-image/{poi_id}")
async def place_image(poi_id: str):
    try:
        poi = _mock.get(poi_id)
        image = await _run_blocking(_images.resolve, poi)
    except Exception:  # noqa: BLE001
        return FileResponse(BASE / "static" / "placeholder.png")
    if image.kind == "redirect" and image.url:
        return RedirectResponse(image.url, headers={"Cache-Control": "no-store"})
    if image.kind == "bytes" and image.content:
        return Response(content=image.content, media_type=image.media_type,
                        headers={"Cache-Control": "no-store"})
    return FileResponse(BASE / "static" / "placeholder.png")


# 静态资源放最后挂载，避免吃掉 /api
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
