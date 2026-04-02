"""Configuration loader with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8420


@dataclass
class OllamaConfig:
    base_url: str = "http://localhost:11434"
    default_model: str = "llama3.1:8b"
    request_timeout: int = 120


@dataclass
class DatabaseConfig:
    path: str = "./aiventbus.db"


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
    journald_enabled: bool = False
    journald_filter_noise: bool = True
    journald_priority_filter: int = 4  # 4=warning+, 3=error+, 7=all (auth always passes)
    journald_units: list = field(default_factory=list)  # e.g. ["sshd", "docker"]


@dataclass
class PolicyConfig:
    trust_overrides: dict = field(default_factory=dict)
    shell_timeout_seconds: int = 30


@dataclass
class ClassifierConfig:
    enabled: bool = False
    model: str = "gemma3:latest"
    timeout_seconds: int = 10


@dataclass
class LaneConfig:
    interactive_prefixes: list = field(default_factory=lambda: ["user."])
    critical_prefixes: list = field(default_factory=lambda: ["security.", "system.failure"])
    reserve_interactive_slot: bool = True


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    bus: BusConfig = field(default_factory=BusConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    producers: ProducersConfig = field(default_factory=ProducersConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    lanes: LaneConfig = field(default_factory=LaneConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    seed_defaults: bool = True


def _merge_dict(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load config from YAML file, falling back to defaults."""
    raw: dict = {}

    if config_path is None:
        # Check common locations
        for candidate in ["config.yaml", "config.yml"]:
            if os.path.exists(candidate):
                config_path = candidate
                break

    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

    return AppConfig(
        server=ServerConfig(**raw.get("server", {})),
        ollama=OllamaConfig(**raw.get("ollama", {})),
        database=DatabaseConfig(**raw.get("database", {})),
        bus=BusConfig(**raw.get("bus", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
        producers=ProducersConfig(**raw.get("producers", {})),
        policy=PolicyConfig(**raw.get("policy", {})),
        lanes=LaneConfig(**raw.get("lanes", {})),
        classifier=ClassifierConfig(**raw.get("classifier", {})),
        seed_defaults=raw.get("seed_defaults", True),
    )
