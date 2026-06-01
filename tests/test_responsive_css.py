from pathlib import Path


def _css() -> str:
    return (Path(__file__).resolve().parents[1] / "app/static/app.css").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


def test_desktop_shell_scales_to_viewport_height():
    css = _compact(_css())

    assert "--panel-h:min(858px,calc(100svh-(var(--page-pad)*2)))" in css
    assert ".phone{width:var(--phone-w);height:auto;aspect-ratio:402/858;" in css
    assert ".tech{width:min(392px,calc(var(--phone-w)*392/402));height:var(--panel-h);" in css


def test_base_layout_no_longer_uses_fixed_858px_panels():
    base_css = _css().split("/* responsive:", 1)[0]
    compact = _compact(base_css)

    assert ".phone{width:402px;height:858px;" not in compact
    assert ".tech{width:392px;height:858px;" not in compact
