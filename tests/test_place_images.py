from app.place_images import PlaceImageResolver


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", headers=None):
        self._json = json_data or {}
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params or {}})
        return self.responses.pop(0)


def test_resolver_prefers_amap_poi_photo_url():
    poi = {"id": "kiddo_lab", "name": "河西 Kiddo Lab",
           "category": "亲子乐园", "location": [118.7372, 32.0148]}
    client = FakeClient([
        FakeResponse(json_data={
            "status": "1",
            "pois": [{"name": "河西 Kiddo Lab", "photos": [{"url": "https://img.example/kiddo.jpg"}]}],
        })
    ])

    image = PlaceImageResolver(amap_key="test-key", client=client).resolve(poi)

    assert image.kind == "redirect"
    assert image.url == "https://img.example/kiddo.jpg"
    assert client.calls[0]["url"] == "https://restapi.amap.com/v5/place/text"
    assert client.calls[0]["params"]["show_fields"] == "photos"
    assert client.calls[0]["params"]["keywords"] == "河西 Kiddo Lab"


def test_resolver_rejects_unrelated_poi_photo_url():
    poi = {"id": "kiddo_lab", "name": "河西 Kiddo Lab",
           "category": "亲子乐园", "location": [118.7372, 32.0148]}
    client = FakeClient([
        FakeResponse(json_data={
            "status": "1",
            "pois": [{"name": "AMD研发中心", "photos": [{"url": "https://img.example/amd.jpg"}]}],
        }),
        FakeResponse(json_data={
            "status": "1",
            "pois": [{"name": "南京亲子乐园", "photos": [{"url": "https://img.example/play.jpg"}]}],
        }),
    ])

    image = PlaceImageResolver(amap_key="test-key", client=client).resolve(poi)

    assert image.kind == "redirect"
    assert image.url == "https://img.example/play.jpg"
    assert client.calls[1]["params"]["keywords"] == "南京 亲子乐园"


def test_resolver_falls_back_to_empty_when_no_photo():
    poi = {"id": "lake_walk", "name": "玄武湖环湖漫步",
           "category": "街区漫步", "location": [118.7911, 32.0756]}
    client = FakeClient([
        FakeResponse(json_data={"status": "1", "pois": [{"photos": []}]}),
        FakeResponse(json_data={"status": "1", "pois": [{"photos": []}]}),
    ])

    image = PlaceImageResolver(amap_key="test-key", client=client).resolve(poi)

    assert image.kind == "empty"
    assert client.calls[1]["url"] == "https://restapi.amap.com/v5/place/text"
    assert client.calls[1]["params"]["keywords"] == "南京 街区漫步"


def test_resolver_uses_local_place_photo_when_amap_has_no_image(tmp_path):
    assets = tmp_path / "place_photos"
    assets.mkdir()
    photo = assets / "kiddo_lab.jpg"
    photo.write_bytes(b"real-photo")

    resolver = PlaceImageResolver(
        amap_key="",
        local_assets_dir=assets,
        local_fallbacks={"kiddo_lab": "kiddo_lab.jpg"},
    )

    image = resolver.resolve({"id": "kiddo_lab", "name": "河西 Kiddo Lab"})

    assert image.kind == "bytes"
    assert image.content == b"real-photo"
    assert image.media_type == "image/jpeg"


def test_default_client_ignores_unsupported_system_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:7890")

    resolver = PlaceImageResolver(amap_key="test-key")

    assert resolver.client is not None
    resolver.client.close()
