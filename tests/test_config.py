from __future__ import annotations

import os

import pytest

from kimi_agent.config import (
    ConfigError,
    Settings,
    coerce_value,
    finalize,
    load_dotenv,
    load_settings,
    missing_api_key_message,
)


def write_config(home, text):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text)


def test_defaults_are_kimi(home, workdir):
    settings, warnings = load_settings()
    assert settings.provider == "kimi" and settings.base_url == "https://api.moonshot.ai/v1"
    assert settings.model and settings.context_window > 100_000 and settings.vision is True
    assert settings.approval == "auto" and settings.workdir == str(workdir)
    assert settings.data_dir == str(home) and warnings == []
    assert "KIMI_API_KEY" in missing_api_key_message(settings)


def test_api_key_from_environment(monkeypatch, workdir):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    settings, _ = load_settings()
    assert settings.api_key == "sk-moon" and missing_api_key_message(settings) is None


def test_local_providers_need_no_key(workdir):
    settings, _ = load_settings({"provider": "ollama"})
    assert settings.base_url == "http://localhost:11434/v1" and settings.api_key == "not-needed"
    assert missing_api_key_message(settings) is None


def test_precedence_toml_env_cli(home, workdir, monkeypatch):
    write_config(home, 'provider = "openrouter"\nmodel = "toml-model"\napproval = "ask"\nmax_steps = 7\n'
                       'allow_commands = ["pytest"]\nbogus_key = 1\n[extra_body]\na = 1\n')
    monkeypatch.setenv("KIMI_AGENT_APPROVAL", "yolo")
    monkeypatch.setenv("KIMI_AGENT_ALLOW_COMMANDS", "pytest, npm test")
    monkeypatch.setenv("KIMI_AGENT_EXTRA_BODY", '{"b": 2}')
    settings, warnings = load_settings({"max_steps": 3, "model": None})
    assert settings.provider == "openrouter" and settings.model == "toml-model"
    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.approval == "yolo"  # env beats TOML
    assert settings.max_steps == 3  # CLI beats TOML
    assert settings.allow_commands == ["pytest", "npm test"]
    assert settings.extra_body == {"a": 1, "b": 2}  # dicts are merged across sources
    assert any("bogus_key" in w for w in warnings)


def test_cli_provider_switch_resets_connection_settings(home, workdir):
    write_config(home, 'provider = "openrouter"\nmodel = "moonshotai/kimi-k3"\ncontext_window = 1000\n')
    settings, _ = load_settings({"provider": "ollama"})
    assert settings.model == "qwen3:8b" and settings.base_url == "http://localhost:11434/v1"
    assert settings.context_window == 32_768
    settings, _ = load_settings({"provider": "ollama", "model": "llama3.3"})
    assert settings.model == "llama3.3"


def test_custom_provider_from_toml(home, workdir, monkeypatch):
    write_config(home, '[providers.mybox]\nbase_url = "http://gpu-box:8000/v1/"\nmodel = "kimi-local"\n'
                       'api_key_env = "MYBOX_KEY"\ncontext_window = 64000\nneeds_key = true\n')
    monkeypatch.setenv("MYBOX_KEY", "secret")
    settings, _ = load_settings({"provider": "mybox"})
    assert (settings.base_url, settings.model, settings.api_key, settings.context_window) == (
        "http://gpu-box:8000/v1", "kimi-local", "secret", 64000)


def test_explicit_config_file(tmp_path, workdir):
    path = tmp_path / "other.toml"
    path.write_text('approval = "ask"\n')
    assert load_settings(config_file=str(path))[0].approval == "ask"
    with pytest.raises(ConfigError, match="not found"):
        load_settings(config_file=str(tmp_path / "missing.toml"))
    path.write_text("approval = \n")
    with pytest.raises(ConfigError, match="Could not read"):
        load_settings(config_file=str(path))


def test_dotenv_is_loaded(workdir, monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "placeholder")
    monkeypatch.delenv("KIMI_API_KEY")  # restore "unset" after the test even though .env sets it
    (workdir / ".env").write_text('# comment\nexport KIMI_API_KEY="from-dotenv"\n')
    assert load_settings()[0].api_key == "from-dotenv"


def test_dotenv_syntax(tmp_path, monkeypatch):
    names = ["DOT_A", "DOT_B", "DOT_C", "DOT_D", "DOT_E", "DOT_F", "DOT_G"]
    for name in names:
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setenv("DOT_G", "already set")
    env = tmp_path / ".env"
    env.write_text("DOT_A=plain # inline comment\nDOT_B='single # kept' # comment\nDOT_C= # only a comment\n"
                   'DOT_D=#not-a-comment\nDOT_E="a=b"\nDOT_F=\nDOT_G=from-file\nnot a pair\n')
    load_dotenv(env)
    assert [os.environ[name] for name in names] == ["plain", "single # kept", "", "#not-a-comment", "a=b", "",
                                                    "already set"]


def test_empty_env_values_are_ignored(workdir, monkeypatch):
    monkeypatch.setenv("KIMI_AGENT_PROVIDER", "")
    monkeypatch.setenv("KIMI_AGENT_MODEL", "  ")
    monkeypatch.setenv("KIMI_AGENT_PLUGINS", "")
    settings = load_settings()[0]
    assert (settings.provider, settings.model, settings.plugins) == ("kimi", "kimi-k3", [])


def test_kimi_base_url_env(workdir, monkeypatch):
    monkeypatch.setenv("KIMI_BASE_URL", "https://proxy.example/v1")
    assert load_settings()[0].base_url == "https://proxy.example/v1"


@pytest.mark.parametrize("field, value", [("approval", "sometimes"), ("tool_mode", "magic"), ("provider", "nope")])
def test_finalize_validation(field, value):
    settings = Settings(**{field: value})
    with pytest.raises(ConfigError):
        finalize(settings)


def test_custom_without_base_url():
    with pytest.raises(ConfigError, match="base URL"):
        finalize(Settings(provider="custom", model="x"))


def test_coerce_value():
    assert coerce_value("stream", "off") is False and coerce_value("web", "YES") is True
    assert coerce_value("max_steps", " 12 ") == 12 and coerce_value("temperature", "0.5") == 0.5
    assert coerce_value("temperature", 1) == 1.0 and coerce_value("temperature", "none") is None
    assert coerce_value("deny_commands", '["a", "b"]') == ["a", "b"]
    assert coerce_value("extra_headers", '{"X": "1"}') == {"X": "1"}
    for name, bad in [("stream", "maybe"), ("max_steps", "many"), ("extra_body", "{oops")]:
        with pytest.raises(ConfigError):
            coerce_value(name, bad)
