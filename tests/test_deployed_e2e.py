import os
import json
import subprocess

import pytest


BASE_URL = os.environ.get("DEPLOYED_BASE_URL", "").rstrip("/")


pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="set DEPLOYED_BASE_URL to run deployed end-to-end checks",
)


def _request(method: str, path: str, payload: dict | None = None):
    command = [
        "curl",
        "-sS",
        "--max-time",
        "35",
        "--retry",
        "3",
        "--retry-all-errors",
        "--retry-delay",
        "1",
        "-w",
        "\n%{http_code}",
        "-X",
        method,
    ]
    if payload is not None:
        command.extend([
            "-H",
            "content-type: application/json",
            "--data",
            json.dumps(payload, ensure_ascii=False),
        ])
    command.append(f"{BASE_URL}{path}")
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    body, _, status_text = completed.stdout.rpartition("\n")
    return int(status_text), body


def _json_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    status, body = _request(method, path, payload)
    return status, json.loads(body)


def test_deployed_health_uses_kimi_model():
    status, data = _json_request("GET", "/api/health")

    assert status == 200
    assert data["ok"] is True
    assert data["model"] == "kimi-k2.6"
    assert data["merchants"] >= 33


def test_deployed_homepage_exposes_brand_and_persona():
    status, body = _request("GET", "/")

    assert status == 200
    assert "本地引力" in body
    assert "小团" in body
    assert "trycloudflare" not in body


def test_deployed_custom_query_returns_first_card_without_timeout():
    payload = {
        "query": "一个人，现在去，新街口附近，想吃清淡一点，也想安静坐一会儿",
        "source": "custom",
    }
    status, data = _json_request("POST", "/api/start", payload)

    assert status == 200
    assert data["constraints"]["scene"] == "solo"
    assert data["state"]["id"]
    assert data["card"]["poi"]["id"] == "deji_green_tea"
    assert "新街口" in data["card"]["poi"]["match_reasons"]
    assert "清淡" in data["card"]["poi"]["match_reasons"]
    assert data["segments"][0]["kind"] == "eat"


def test_deployed_preset_accept_execute_flow_returns_mock_confirmation():
    status, first = _json_request(
        "POST",
        "/api/start",
        {
            "query": "周末下午带老婆孩子出来玩，孩子5岁，想找个能遛娃的地方，再吃顿好的，别离家太远",
            "source": "preset",
            "preset_id": "family",
        },
    )
    assert status == 200
    assert first["card"]["poi"]["id"] == "kiddo_lab"
    state = first["state"]

    status, second = _json_request("POST", "/api/accept", {"state": state})
    assert status == 200
    assert second["card"]
    state = second["state"]

    status, third = _json_request("POST", "/api/accept", {"state": state})
    assert status == 200
    assert third["card"]
    state = third["state"]

    status, ready = _json_request("POST", "/api/accept", {"state": state})
    assert status == 200
    assert ready["done"] is True
    assert len(ready["plan"]["stops"]) == 3
    state = ready["state"]

    status, result = _json_request("POST", "/api/execute", {"state": state})
    assert status == 200
    assert result["ok"] is True
    assert len(result["results"]) == 3
    assert all(item["status"] == "ok" for item in result["results"])
