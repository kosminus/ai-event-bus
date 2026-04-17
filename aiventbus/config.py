"""Configuration loader.

Path resolution is deterministic and CWD-independent by default. A process
started by ``systemd`` / ``launchd`` with a minimal environment resolves the
same config and DB as an interactive CLI invocation, regardless of where
either was launched from.

Lookup order for the config file path:

    1. ``--config <path>`` flag (via ``load_config(config_path=...)``).
    2. ``$AIVENTBUS_CONFIG`` environment variable.
    3. Dev-mode CWD fallback — only when ``--dev`` / ``$AIVENTBUS_DEV=1`` is
       active, use ``./config.yaml`` (if present).
    4. ``platform.default_config_path()``.

Lookup order for the DB path:

    1. ``--db <path>`` flag (via ``load_config(db_path=...)``).
    2. ``$AIVENTBUS_DB`` environment variable.
    3. ``database.path`` explicitly set in the YAML.
    4. Dev-mode CWD fallback — only when ``--dev`` / ``$AIVENTBUS_DEV=1`` is
       active, use ``./aiventbus.db`` (if present).
    5. ``platform.default_db_path()``.

Callers inspect ``AppConfig.sources`` to report which source was used for
each resolved path (surfaced in ``/api/v1/system/status``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from aiventbus import platform as _platform


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8420


@dataclass
class OllamaConfig:
    base_url: str = "http://localhost:11434"
    default_model: str = "gemma4:latest"
    request_timeout: int = 120


@dataclass
class DatabaseConfig:
    # ``None`` means "not set — resolver will pick the platform default".
    # ``load_config`` always populates this with an absolute path before
    # returning, so downstream code sees a resolved string.
    path: str | None = None


@dataclass
class BusConfig:
    dedupe_window_seconds: int = 60
    max_fan_out: int = 3
    max_chain_depth: int = 10
    max_chain_budget: int = 20
    backpressure_strategy: str = "drop_low"  # drop_low | summarize | block


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class ProducersConfig:
    clipboard_enabled: bool = True
    clipboard_poll_interval_ms: int = 500
    clipboard_min_length: int = 10
    file_watcher_enabled: bool = False
    file_watcher_paths: list = field(default_factory=lambda: ["~/Downloads", "~/Documents"])
    dbus_enabled: bool = False
    terminal_monitor_enabled: bool = False
    terminal_history_path: str | None = None
    # journald_* controls the unified system_log producer. Kept under the
    # journald prefix for config compatibility; on macOS the same knobs
    # drive the `log stream` backend.
    journald_enabled: bool = False
    journald_filter_noise: bool = True
    journald_priority_filter: int = 4  # 4=warning+, 3=error+, 7=all (auth always passes)
    journald_units: list = field(default_factory=list)  # journald-only: `-u <unit>`
    # Optional override for the macOS `log stream` predicate. Leave None
    # to use the sensible default (errors/faults + auth subsystems).
    log_stream_predicate: str | None = None
    webhook_enabled: bool = False
    webhook_secret: str | None = None  # Bearer token / HMAC secret (None = no auth)
    webhook_default_priority: str = "medium"
    cron_enabled: bool = False
    cron_timezone: str = "UTC"
    cron_jobs: list = field(default_factory=list)  # [{"name": "...", "expression": "*/5 * * * *", "topic": "..."}]


@dataclass
class ToolsConfig:
    http_request_enabled: bool = True
    http_request_timeout: int = 30
    http_request_max_size: int = 1_048_576  # 1MB response cap
    playwright_enabled: bool = False
    playwright_headless: bool = True
    playwright_timeout: int = 30_000  # ms per action


@dataclass
class PolicyConfig:
    trust_overrides: dict = field(default_factory=dict)
    shell_timeout_seconds: int = 30


@dataclass
class ClassifierConfig:
    enabled: bool = False
    model: str = "gemma4:latest"
    timeout_seconds: int = 10


@dataclass
class LaneConfig:
    interactive_prefixes: list = field(default_factory=lambda: ["user."])
    critical_prefixes: list = field(default_factory=lambda: ["security.", "system.failure"])
    reserve_interactive_slot: bool = True


@dataclass
class ConfigSources:
    """Where each resolved path came from. Exposed via the system-status API."""

    # "cli" | "env" | "dev_cwd" | "platform_default" | "missing"
    config_path_source: str = "missing"
    # absolute path when found; ``None`` when no config file was loaded.
    config_path: str | None = None

    # "cli" | "env" | "yaml" | "dev_cwd" | "platform_default"
    db_path_source: str = "platform_default"
    db_path: str = ""

    dev_mode: bool = False


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    bus: BusConfig = field(default_factory=BusConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    producers: ProducersConfig = field(default_factory=ProducersConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    lanes: LaneConfig = field(default_factory=LaneConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    seed_defaults: bool = True
    # Transient: populated by ``load_config``. Not persisted to YAML.
    sources: ConfigSources = field(default_factory=ConfigSources)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _is_dev_mode(dev: bool | None) -> bool:
    if dev is True:
        return True
    if dev is False:
        return False
    val = os.environ.get("AIVENTBUS_DEV", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _resolve_config_path(
    cli_path: str | Path | None, dev: bool
) -> tuple[Path | None, str]:
    """Apply the 4-step lookup for the config file path."""
    if cli_path:
        return Path(cli_path).expanduser(), "cli"

    env_val = os.environ.get("AIVENTBUS_CONFIG")
    if env_val:
        return Path(env_val).expanduser(), "env"

    if dev:
        cwd_candidate = Path.cwd() / "config.yaml"
        if cwd_candidate.is_file():
            return cwd_candidate, "dev_cwd"
        cwd_alt = Path.cwd() / "config.yml"
        if cwd_alt.is_file():
            return cwd_alt, "dev_cwd"

    default = _platform.default_config_path()
    if default.is_file():
        return default, "platform_default"

    return None, "missing"


def _resolve_db_path(
    cli_db: str | Path | None,
    yaml_db: str | None,
    dev: bool,
) -> tuple[Path, str]:
    """Apply the 5-step lookup for the DB path."""
    if cli_db:
        return Path(cli_db).expanduser(), "cli"

    env_val = os.environ.get("AIVENTBUS_DB")
    if env_val:
        return Path(env_val).expanduser(), "env"

    if yaml_db:
        return Path(yaml_db).expanduser(), "yaml"

    if dev:
        cwd_candidate = Path.cwd() / "aiventbus.db"
        if cwd_candidate.exists():
            return cwd_candidate, "dev_cwd"

    return _platform.default_db_path(), "platform_default"


def _merge_dict(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    dev: bool | None = None,
) -> AppConfig:
    """Load config from YAML, applying the deterministic path resolver.

    All three arguments are explicit overrides that win over environment
    variables; leave them at their defaults to let the standard lookup
    order apply.
    """
    dev_mode = _is_dev_mode(dev)

    resolved_config_path, config_source = _resolve_config_path(config_path, dev_mode)

    raw: dict = {}
    if resolved_config_path is not None:
        try:
            with open(resolved_config_path) as f:
                raw = yaml.safe_load(f) or {}
        except OSError:
            # If the explicit path is broken we still want to fail loudly.
            if config_source in ("cli", "env"):
                raise
            raw = {}

    yaml_db = None
    db_section = raw.get("database", {}) or {}
    if isinstance(db_section, dict):
        yaml_db = db_section.get("path")

    resolved_db, db_source = _resolve_db_path(db_path, yaml_db, dev_mode)
    # Ensure the parent directory exists for platform-default placements.
    if db_source == "platform_default":
        resolved_db.parent.mkdir(parents=True, exist_ok=True)

    # Build the AppConfig. We intentionally override ``database.path`` with
    # the resolved value so downstream code sees a fully-qualified string.
    cfg = AppConfig(
        server=ServerConfig(**raw.get("server", {})),
        ollama=OllamaConfig(**raw.get("ollama", {})),
        database=DatabaseConfig(path=str(resolved_db)),
        bus=BusConfig(**raw.get("bus", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
        producers=ProducersConfig(**raw.get("producers", {})),
        policy=PolicyConfig(**raw.get("policy", {})),
        tools=ToolsConfig(**raw.get("tools", {})),
        lanes=LaneConfig(**raw.get("lanes", {})),
        classifier=ClassifierConfig(**raw.get("classifier", {})),
        seed_defaults=raw.get("seed_defaults", True),
    )
    cfg.sources = ConfigSources(
        config_path_source=config_source,
        config_path=str(resolved_config_path) if resolved_config_path else None,
        db_path_source=db_source,
        db_path=str(resolved_db),
        dev_mode=dev_mode,
    )
    return cfg
