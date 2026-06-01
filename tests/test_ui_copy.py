from pathlib import Path


def test_user_visible_copy_does_not_expose_internal_self_heal_terms():
    root = Path(__file__).resolve().parents[1]
    visible_files = [
        root / "app" / "static" / "app.js",
        root / "app" / "static" / "app.css",
        root / "app" / "templates" / "index.html",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in visible_files)

    assert "已自愈" not in text
    assert "异常自愈" not in text
    assert "自愈成功" not in text
    assert "自愈" not in text


def test_card_renderer_does_not_duplicate_body_wrapper():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert '<div class="body">\\n    <div class="body">' not in js


def test_card_meta_labels_are_clipped_inside_fixed_cells():
    root = Path(__file__).resolve().parents[1]
    css = (root / "app" / "static" / "app.css").read_text(encoding="utf-8")
    compact = "".join(css.split())

    assert ".card.metaspan{min-width:0;height:28px;display:block;" in compact
    assert ".d-metaspan{min-width:0;height:28px;display:block;" in compact
    assert ".card.metaspan" in compact and "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" in compact


def test_agent_replay_honors_event_specific_delay():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert "ev.delay_ms" in js
    assert "Math.max(0, Number(ev.delay_ms" in js


def test_preset_loading_copy_uses_normal_thinking_language():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert "正在读取预设" not in js
    assert "正在读取预设场景" not in js
    assert "正在思考中" in js


def test_start_reply_without_card_keeps_intake_open():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert "d.needs_more_info && !d.card" in js
    assert "state.active = true" in js
    assert "state.intake = true" in js
    assert "addAssistantBubble(d.reply)" in js


def test_chat_can_render_segments_when_intake_agent_finishes_planning():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert "if (d.segments && d.segments.length)" in js
    assert "renderNarrative(d.narrative, d.segments, d.current_index || 0)" in js
    assert "state.intake = false" in js


def test_intake_option_cards_are_rendered_as_clickable_choices():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert "renderIntakeOptions(d.intake_options)" in js
    assert "function renderIntakeOptions" in js
    assert "btn.onclick = () => sendChat(opt.value)" in js
    assert "clearIntakeOptions()" in js
    assert ".intake-options" in css
    assert ".intake-card" in css
    assert ".intake-choice" in css
