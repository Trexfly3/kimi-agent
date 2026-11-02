"""Settings, provider presets and configuration loading.

Precedence (highest first): CLI flags > KIMI_AGENT_* environment variables >
--config file > ~/.kimi-agent/config.toml > provider preset > defaults.
"""

from __future__ import annotations

import json
import os
import re
import sys
import types
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ENV_PREFIX = "KIMI_AGENT_"
APPROVAL_MODES = ("ask", "auto", "yolo")
TOOL_MODES = ("auto", "native", "text")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    model: str
    key_envs: tuple[str, ...] = ()
    context_window: int = 128_000
    vision: bool = False
    needs_key: bool = True
    kimi: bool = False  # official Kimi API: supports builtin $web_search, thinking params
    description: str = ""


PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in [
        Provider("kimi", "https://api.moonshot.ai/v1", "kimi-k3", ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
                 1_000_000, vision=True, kimi=True, description="Moonshot AI Kimi API (international)"),
        Provider("kimi-cn", "https://api.moonshot.cn/v1", "kimi-k3", ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
                 1_000_000, vision=True, kimi=True, description="Moonshot AI Kimi API (mainland China)"),
        Provider("kimi-code", "https://api.kimi.com/coding/v1", "kimi-for-coding",
                 ("KIMI_CODE_API_KEY", "KIMI_API_KEY"), 256_000, vision=True, kimi=True,
                 description="Kimi Code subscription endpoint"),
        Provider("openrouter", "https://openrouter.ai/api/v1", "moonshotai/kimi-k3", ("OPENROUTER_API_KEY",),
                 256_000, vision=True, description="OpenRouter (hundreds of open models)"),
        Provider("huggingface", "https://router.huggingface.co/v1", "moonshotai/Kimi-K2-Instruct-0905",
                 ("HF_TOKEN", "HUGGINGFACE_API_KEY"), 256_000, description="Hugging Face Inference Providers"),
        Provider("groq", "https://api.groq.com/openai/v1", "moonshotai/kimi-k2-instruct-0905", ("GROQ_API_KEY",),
                 256_000, description="Groq"),
        Provider("together", "https://api.together.xyz/v1", "moonshotai/Kimi-K2-Instruct-0905",
                 ("TOGETHER_API_KEY",), 256_000, description="Together AI"),
        Provider("fireworks", "https://api.fireworks.ai/inference/v1",
                 "accounts/fireworks/models/kimi-k2-instruct-0905", ("FIREWORKS_API_KEY",), 256_000,
                 description="Fireworks AI"),
        Provider("deepseek", "https://api.deepseek.com/v1", "deepseek-chat", ("DEEPSEEK_API_KEY",), 128_000,
                 description="DeepSeek API"),
        Provider("ollama", "http://localhost:11434/v1", "qwen3:8b", (), 32_768, needs_key=False,
                 description="Local Ollama server"),
        Provider("vllm", "http://localhost:8000/v1", "moonshotai/Kimi-K3", ("VLLM_API_KEY",), 128_000,
                 needs_key=False, description="Self-hosted vLLM server"),
        Provider("sglang", "http://localhost:30000/v1", "moonshotai/Kimi-K3", (), 128_000, needs_key=False,
                 description="Self-hosted SGLang server"),
        Provider("lmstudio", "http://localhost:1234/v1", "local-model", (), 32_768, needs_key=False,
                 description="LM Studio local server"),
        Provider("llamacpp", "http://localhost:8080/v1", "local-model", (), 32_768, needs_key=False,
                 description="llama.cpp server (llama-server)"),
        Provider("custom", "", "", ("OPENAI_API_KEY",), 128_000, needs_key=False,
                 description="Any OpenAI-compatible endpoint (set --base-url and --model)"),
    ]
}


@dataclass
class Settings:
    # Model / provider
    provider: str = "kimi"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    context_window: int = 0
    compact_threshold: float = 0.8
    tool_mode: str = "auto"
    vision: bool | None = None
    stream: bool = True
    request_timeout: float = 600.0
    max_retries: int = 3
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Agent behaviour
    approval: str = "auto"
    allow_commands: list[str] = field(default_factory=list)
    deny_commands: list[str] = field(default_factory=list)
    max_steps: int = 100
    max_output_chars: int = 30_000
    command_timeout: int = 300
    show_reasoning: bool = False

    # Tools
    workdir: str = ""
    shell: str = ""
    python: str = ""
    web: bool = True
    search_backend: str = "auto"
    kimi_web_search: bool = False
    computer_use: bool = False
    computer_auto_screenshot: bool = True
    memory: bool = True
    subagents: bool = True
    disabled_tools: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Misc
    data_dir: str = ""
    debug: bool = False

    @property
    def preset(self) -> Provider:
        custom = self.providers.get(self.provider)
        if custom is not None:
            key_env = custom.get("api_key_env", ())
            return Provider(
                name=self.provider,
                base_url=custom.get("base_url", ""),
                model=custom.get("model", ""),
                key_envs=(key_env,) if isinstance(key_env, str) else tuple(key_env),
                context_window=int(custom.get("context_window", 128_000)),
                vision=bool(custom.get("vision", False)),
                needs_key=bool(custom.get("needs_key", False)),
                kimi=bool(custom.get("kimi", False)),
                description=custom.get("description", "custom provider"),
            )
        return PROVIDERS.get(self.provider, PROVIDERS["custom"])

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)


def default_data_dir() -> Path:
    return Path(os.environ.get("KIMI_AGENT_HOME") or Path.home() / ".kimi-agent").expanduser()


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides existing variables."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, raw = line.split("=", 1)
        key, value = key.strip(), raw.strip()
        if value[:1] in ("'", '"') and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]
        elif value.startswith("#") and raw[:1].isspace():
            value = ""  # KEY= # comment
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0]  # inline comment
        if key:
            os.environ.setdefault(key, value)


_HINTS: dict[str, Any] | None = None


def _hints() -> dict[str, Any]:
    global _HINTS
    if _HINTS is None:
        _HINTS = get_type_hints(Settings)
    return _HINTS


def _unwrap_optional(hint: Any) -> tuple[Any, bool]:
    if get_origin(hint) in (Union, types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        return (args[0] if len(args) == 1 else hint), True
    return hint, False


def coerce_value(name: str, value: Any) -> Any:
    """Convert a raw value (string from env/CLI, or TOML value) to the field's type."""
    hint, optional = _unwrap_optional(_hints()[name])
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if optional and raw.lower() in ("", "none", "null"):
            return None
        if hint is bool:
            if raw.lower() in ("1", "true", "yes", "on"):
                return True
            if raw.lower() in ("0", "false", "no", "off"):
                return False
            raise ConfigError(f"{name}: expected a boolean, got {value!r}")
        if hint is int:
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name}: expected an integer, got {value!r}") from exc
        if hint is float:
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name}: expected a number, got {value!r}") from exc
        origin = get_origin(hint)
        if origin is list:
            if raw.startswith("["):
                try:
                    return list(json.loads(raw))
                except ValueError as exc:
                    raise ConfigError(f"{name}: expected a JSON list, got {value!r}") from exc
            return [s.strip() for s in raw.split(",") if s.strip()]
        if origin is dict:
            try:
                return dict(json.loads(raw))
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"{name}: expected a JSON object, got {value!r}") from exc
        return value
    if hint is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _apply(settings: Settings, data: dict[str, Any], source: str, warnings: list[str]) -> None:
    names = {f.name for f in fields(Settings)}
    for key, value in data.items():
        key = key.replace("-", "_")
        if key not in names:
            warnings.append(f"{source}: unknown setting '{key}' ignored")
            continue
        value = coerce_value(key, value)
        if key in ("mcp_servers", "providers", "extra_body", "extra_headers") and isinstance(value, dict):
            value = {**getattr(settings, key), **value}  # dicts from later sources extend earlier ones
        setattr(settings, key, value)


def load_settings(
    overrides: dict[str, Any] | None = None,
    config_file: str | None = None,
) -> tuple[Settings, list[str]]:
    """Build Settings from all sources. Returns (settings, warnings)."""
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    warnings: list[str] = []
    data_dir = Path(overrides.get("data_dir") or default_data_dir()).expanduser()

    load_dotenv(Path.cwd() / ".env")
    load_dotenv(data_dir / ".env")

    settings = Settings()
    candidates: list[tuple[Path, bool]] = [(data_dir / "config.toml", False)]
    if config_file:
        candidates.append((Path(config_file).expanduser(), True))
    for path, explicit in candidates:
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError) as exc:
                raise ConfigError(f"Could not read config file {path}: {exc}") from exc
            _apply(settings, data, str(path), warnings)
        elif explicit:
            raise ConfigError(f"Config file not found: {path}")

    env_data = {}
    for f in fields(Settings):
        env_value = os.environ.get(ENV_PREFIX + f.name.upper())
        if env_value is not None and env_value.strip():  # empty means unset, e.g. "KIMI_AGENT_MODEL=" in .env
            env_data[f.name] = env_value
    _apply(settings, env_data, "environment", warnings)
    if overrides.get("provider") and overrides["provider"] != settings.provider:
        # Connection settings configured for another provider must not leak into this one.
        defaults = Settings()
        for key in ("base_url", "model", "api_key", "context_window", "vision"):
            if key not in overrides:
                setattr(settings, key, getattr(defaults, key))
    _apply(settings, overrides, "command line", warnings)

    if not settings.data_dir:
        settings.data_dir = str(data_dir)
    finalize(settings)
    return settings, warnings


def finalize(settings: Settings) -> None:
    """Fill unset values from the provider preset and validate."""
    if settings.approval not in APPROVAL_MODES:
        raise ConfigError(f"approval must be one of {', '.join(APPROVAL_MODES)} (got {settings.approval!r})")
    if settings.tool_mode not in TOOL_MODES:
        raise ConfigError(f"tool_mode must be one of {', '.join(TOOL_MODES)} (got {settings.tool_mode!r})")
    if settings.provider not in PROVIDERS and settings.provider not in settings.providers:
        known = ", ".join(list(PROVIDERS) + list(settings.providers))
        raise ConfigError(f"Unknown provider {settings.provider!r}. Known providers: {known}")

    preset = settings.preset
    if not settings.base_url:
        if preset.kimi and os.environ.get("KIMI_BASE_URL") and preset.name == "kimi":
            settings.base_url = os.environ["KIMI_BASE_URL"]
        else:
            settings.base_url = preset.base_url
    settings.base_url = settings.base_url.rstrip("/")
    if not settings.model:
        settings.model = preset.model
    if not settings.api_key:
        for env in preset.key_envs:
            if os.environ.get(env):
                settings.api_key = os.environ[env]
                break
    if not settings.api_key and not preset.needs_key:
        settings.api_key = "not-needed"
    if not settings.context_window:
        settings.context_window = preset.context_window
    if settings.vision is None:
        settings.vision = preset.vision
    settings.workdir = str(Path(settings.workdir or os.getcwd()).expanduser().resolve())
    if not Path(settings.workdir).is_dir():
        raise ConfigError(f"Working directory {settings.workdir} does not exist.")
    if not settings.data_dir:
        settings.data_dir = str(default_data_dir())
    settings.data_dir = str(Path(settings.data_dir).expanduser())

    if not settings.base_url:
        raise ConfigError("No base URL configured. Pass --base-url or choose a --provider.")
    if not settings.model:
        raise ConfigError("No model configured. Pass --model.")


def missing_api_key_message(settings: Settings) -> str | None:
    preset = settings.preset
    if settings.api_key or not preset.needs_key:
        return None
    envs = " or ".join(preset.key_envs) or "KIMI_AGENT_API_KEY"
    return (
        f"No API key found for provider '{settings.provider}'. Set {envs} (environment or .env), "
        f"KIMI_AGENT_API_KEY, api_key in {default_data_dir() / 'config.toml'}, or pass --api-key."
    )
