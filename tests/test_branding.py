from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_brand_uses_bendi_yinli_and_not_old_buyu_name():
    public_files = [
        ROOT / "README.md",
        ROOT / "pyproject.toml",
        ROOT / "app" / "__init__.py",
        ROOT / "app" / "server.py",
        ROOT / "app" / "session.py",
        ROOT / "app" / "prompts.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in public_files)

    assert "本地引力" in text
    assert "步语 BuYu" not in text


def test_xiaotuan_persona_copy_is_preserved():
    persona_files = [
        ROOT / "app" / "templates" / "index.html",
        ROOT / "app" / "static" / "app.js",
        ROOT / "app" / "session.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in persona_files)

    assert "小团" in text
