import os

import httpx
import pytest


BASE_URL = os.environ.get("DEPLOYED_BASE_URL", "").rstrip("/")


pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="set DEPLOYED_BASE_URL to run deployed end-to-end checks",
)


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=35.0, follow_redirects=True)


def test_deployed_health_uses_kimi_model():
    with _client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["model"] == "kimi-k2.6"
    assert data["merchants"] >= 33


def test_deployed_homepage_exposes_brand_and_persona():
    with _client() as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "本地引力" in response.text
    assert "小团" in response.text
    assert "trycloudflare" not in response.text


def test_deployed_custom_query_returns_first_card_without_timeout():
    payload = {
        "query": "一个人，现在去，新街口附近，想吃清淡一点，也想安静坐一会儿",
        "source": "custom",
    }
    with _client() as client:
        response = client.post("/api/start", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["constraints"]["scene"] == "solo"
    assert data["card"]["poi"]["id"] == "deji_green_tea"
    assert "新街口" in data["card"]["poi"]["match_reasons"]
    assert "清淡" in data["card"]["poi"]["match_reasons"]
    assert data["segments"][0]["kind"] == "eat"


def test_deployed_preset_accept_execute_flow_returns_mock_confirmation():
    with _client() as client:
        start = client.post(
            "/api/start",
            json={
                "query": "周末下午带老婆孩子出来玩，孩子5岁，想找个能遛娃的地方，再吃顿好的，别离家太远",
                "source": "preset",
                "preset_id": "family",
            },
        )
        assert start.status_code == 200
        first = start.json()
        assert first["card"]["poi"]["id"] == "kiddo_lab"

        second = client.post("/api/accept")
        assert second.status_code == 200
        assert second.json()["card"]

        third = client.post("/api/accept")
        assert third.status_code == 200
        assert third.json()["card"]

        final_accept = client.post("/api/accept")
        assert final_accept.status_code == 200
        ready = final_accept.json()
        assert ready["done"] is True
        assert len(ready["plan"]["stops"]) == 3

        execute = client.post("/api/execute")
        assert execute.status_code == 200
        result = execute.json()
        assert result["done"] is True
        assert len(result["executions"]) == 3
