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
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    bus: BusConfig = field(default_factory=BusConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


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
    )
