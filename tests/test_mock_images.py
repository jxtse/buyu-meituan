from app.mock.meituan import MeituanMock
from app.mock.meituan import DATA_PATH


STATIC_ROOT = DATA_PATH.parents[2] / "static"


def test_mock_image_ref_serves_local_static_asset_from_library():
    mock = MeituanMock()

    kiddo = mock.search_merchants(segment="play", scene="family")[0]

    assert kiddo["id"] == "kiddo_lab"
    assert kiddo["image"].startswith("/static/place_photos/")
    assert "place-image" not in kiddo["image"]


def test_all_mock_library_images_exist_on_disk():
    mock = MeituanMock()

    for item in mock._items:
        paths = [item["image"], *(item.get("gallery") or [])]
        for note in item.get("dianping_notes") or []:
            paths.extend(note.get("images") or [])

        for path in paths:
            assert path.startswith("place_photos/"), path
            assert (STATIC_ROOT / path).exists(), path


def test_each_merchant_has_distinct_primary_image():
    mock = MeituanMock()

    images = [item["image"] for item in mock._items]

    assert len(images) == len(set(images))


def test_each_dianping_note_has_local_cover_image():
    mock = MeituanMock()

    for item in mock._items:
        for note in item.get("dianping_notes") or []:
            assert note.get("images"), (item["id"], note.get("title"))
            for path in note["images"]:
                assert path.startswith("place_photos/"), path
                assert (STATIC_ROOT / path).exists(), path
