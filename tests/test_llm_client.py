from app.llm import LLMClient


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class _FakeHTTP:
    def __init__(self):
        self.url = None
        self.body = None

    def post(self, url, json):
        self.url = url
        self.body = json
        return _FakeResponse()


def test_kimi_chat_completions_omits_temperature():
    client = LLMClient(
        base_url="https://api.moonshot.ai",
        api_key="sk-test",
        model="kimi-k2.6",
    )
    fake_http = _FakeHTTP()
    client._http = fake_http

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert fake_http.url == "https://api.moonshot.ai/v1/chat/completions"
    assert fake_http.body["model"] == "kimi-k2.6"
    assert "temperature" not in fake_http.body
