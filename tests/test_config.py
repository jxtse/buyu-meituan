from app.config import load_config


def test_kimi_env_key_uses_moonshot_defaults(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KIMI_API_KEY=sk-test\n", encoding="utf-8")

    cfg = load_config(env_path=env_file)

    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://api.moonshot.ai"
    assert cfg.model == "kimi-k2.6"
    assert cfg.llm_timeout_seconds == 20.0


def test_planner_timeout_can_be_overridden(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KIMI_API_KEY=sk-test\nPLANNER_TIMEOUT_SECONDS=12.5\n",
        encoding="utf-8",
    )

    cfg = load_config(env_path=env_file)

    assert cfg.llm_timeout_seconds == 12.5
