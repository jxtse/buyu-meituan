"""OpenAI 兼容 LLM 客户端。

支持两种 API 形态：
- /v1/chat/completions  经典模型
- /v1/responses         新推理模型（gpt-5.5+，拒绝 chat/completions）

`chat()` 无论走哪条路都返回统一的 `AssistantMessage`。
`chat_json()` 在其上加一层「强制 JSON 输出 + 解析 + 重试」，
推荐流的结构化决策都走它。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.moonshot.ai"
DEFAULT_MODEL = "kimi-k2.6"

_RESPONSES_API_PREFIXES = ("gpt-5.5", "gpt-5.4-mini", "gpt-5.6")
_OMIT_TEMPERATURE_PREFIXES = ("kimi-",)


def _uses_responses_api(model: str) -> bool:
    return any(model.startswith(p) for p in _RESPONSES_API_PREFIXES)


def _omits_temperature(model: str) -> bool:
    return any(model.startswith(p) for p in _OMIT_TEMPERATURE_PREFIXES)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantMessage:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str | None = None


class LLMClient:
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, api_key: str = "",
                 model: str = DEFAULT_MODEL, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers: dict[str, str] = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        # 内网网关在 Tailscale 上，必须绕过系统代理（Clash 会把它转出公网拿 502）。
        self._http = httpx.Client(timeout=timeout, headers=headers, trust_env=False)

    # ---------------- 高层：强制 JSON ----------------
    def chat_json(self, *, system: str, user: str, model: str | None = None,
                  retries: int = 0) -> tuple[dict[str, Any], str | None]:
        """让模型只输出一个 JSON 对象，解析后返回 (data, reasoning_text)。

        失败重试 `retries` 次；全失败抛 ValueError，调用方需有 fallback。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            msg = self.chat(messages=messages, tools=[], model=model)
            raw = (msg.content or "").strip()
            try:
                return _extract_json(raw), msg.reasoning
            except Exception as e:  # noqa: BLE001
                last_err = e
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": "上一次输出不是合法 JSON 对象。请只输出一个 JSON "
                               "对象，不要任何解释或 markdown 代码围栏。",
                })
        raise ValueError(f"chat_json failed after retries: {last_err}")

    # ---------------- 基础 chat ----------------
    def chat(self, *, messages: list[dict], tools: list[dict],
             model: str | None = None) -> AssistantMessage:
        m = model or self.model
        if _uses_responses_api(m):
            return self._chat_responses(messages=messages, tools=tools, model=m)
        return self._chat_completions(messages=messages, tools=tools, model=m)

    def _chat_completions(self, *, messages: list[dict], tools: list[dict],
                          model: str) -> AssistantMessage:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 1600,
        }
        if not _omits_temperature(model):
            body["temperature"] = 0
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        r = self._http.post(f"{self.base_url}/v1/chat/completions", json=body)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        tcs: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc["function"]["arguments"]}
            tcs.append(ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args))
        return AssistantMessage(content=msg.get("content"), tool_calls=tcs,
                                reasoning=msg.get("reasoning_text"))

    def _chat_responses(self, *, messages: list[dict], tools: list[dict],
                        model: str) -> AssistantMessage:
        instructions_parts: list[str] = []
        input_items: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                if isinstance(m.get("content"), str):
                    instructions_parts.append(m["content"])
                continue
            if role == "user":
                input_items.append({"role": "user", "content": m.get("content") or ""})
            elif role == "assistant":
                if m.get("content"):
                    input_items.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    })
            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": m.get("content") or "",
                })

        responses_tools: list[dict] = []
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if fn:
                responses_tools.append({
                    "type": "function", "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                })
            else:
                responses_tools.append(t)

        body: dict[str, Any] = {"model": model, "input": input_items}
        if instructions_parts:
            body["instructions"] = "\n\n".join(instructions_parts)
        if responses_tools:
            body["tools"] = responses_tools
            body["tool_choice"] = "auto"

        r = self._http.post(f"{self.base_url}/v1/responses", json=body)
        r.raise_for_status()
        data = r.json()

        content_text: str | None = data.get("output_text") or None
        tcs: list[ToolCall] = []
        text_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        for item in data.get("output") or []:
            itype = item.get("type")
            if itype == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": item.get("arguments")}
                tcs.append(ToolCall(
                    id=item.get("call_id") or item.get("id") or "",
                    name=item.get("name", ""), arguments=args))
            elif itype == "message":
                for c in item.get("content") or []:
                    if c.get("type") in ("output_text", "text"):
                        text_chunks.append(c.get("text", ""))
            elif itype == "reasoning":
                for c in item.get("summary") or []:
                    if isinstance(c, dict) and c.get("text"):
                        reasoning_chunks.append(c["text"])
                    elif isinstance(c, str):
                        reasoning_chunks.append(c)
        if text_chunks and not content_text:
            content_text = "".join(text_chunks)
        reasoning = "".join(reasoning_chunks) or None
        return AssistantMessage(content=content_text, tool_calls=tcs, reasoning=reasoning)


def _extract_json(raw: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象。容忍 ```json 围栏和前后噪声。"""
    text = raw.strip()
    # 去掉 markdown 围栏
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 退而求其次：找第一个 { 到匹配的 }
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                    break
    raise ValueError(f"no JSON object in output: {raw[:200]!r}")
