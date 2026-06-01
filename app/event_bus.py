"""极简线程安全事件总线，给 SSE 推送 + 技术面板消费用。

asyncio.Queue 非线程安全，工作线程发事件必须走 loop.call_soon_threadsafe。
EventBus 封死这个套路：publish() 可在任意线程调用，subscribe() 给每个
/events 连接一条独立队列。可选 event_hook 用于服务端内部消费（如技术面板聚合）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable


class EventBus:
    def __init__(self, *, event_hook: Callable[[dict], None] | None = None) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hook = event_hook

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        if self._hook is not None:
            try:
                self._hook(event)
            except Exception:
                pass
        if self._loop is None:
            # 未绑定 loop（如纯单测）：直接丢进队列
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except Exception:
                    pass
            return
        for q in list(self._subscribers):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                pass
