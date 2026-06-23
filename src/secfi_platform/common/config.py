"""
Configuration loader.

Pattern: a `base.yaml` holds defaults common to every environment; an
environment-specific file (`dev.yaml`, `prod.yaml`) overrides selected
keys. Secrets are NEVER stored in these files — only environment variable
*names* are stored, and resolved at load time from the process
environment / secret manager injection (see infra/docker, .env.example).

This mirrors how a bank platform team would actually manage config:
versioned, environment-layered, secrets externalized, and auditable
(every config object stamps its source files and load time).
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"


class ConfigError(Exception):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_env_placeholders(node: Any) -> Any:
    """
    Recursively resolve strings of the form '${ENV_VAR_NAME}' to the value
    of the named environment variable. Raises ConfigError if a referenced
    secret/env var is required (no default) and missing — fails loudly
    rather than silently running with a missing credential.
    """
    if isinstance(node, dict):
        return {k: _resolve_env_placeholders(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_env_placeholders(v) for v in node]
    if isinstance(node, str) and node.startswith("${") and node.endswith("}"):
        var_name = node[2:-1]
        default = None
        if ":-" in var_name:
            var_name, default = var_name.split(":-", 1)
        value = os.environ.get(var_name, default)
        if value is None:
            raise ConfigError(
                f"Required environment variable '{var_name}' is not set and "
                f"has no default. Refusing to start with an unresolved secret reference."
            )
        return value
    return node


@dataclass
class PlatformConfig:
    """Frozen-at-load-time configuration snapshot, stamped for audit purposes."""
    raw: dict
    environment: str
    loaded_at: datetime
    source_files: tuple[str, ...]

    def get(self, dotted_path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted_path: str) -> Any:
        sentinel = object()
        value = self.get(dotted_path, sentinel)
        if value is sentinel:
            raise ConfigError(f"Required config key '{dotted_path}' is missing.")
        return value


def load_config(environment: str = "dev", config_dir: Path | None = None) -> PlatformConfig:
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    base_path = config_dir / "base.yaml"
    env_path = config_dir / f"{environment}.yaml"

    if not base_path.exists():
        raise ConfigError(f"Missing base config at {base_path}")

    with open(base_path, "r") as f:
        merged = yaml.safe_load(f) or {}

    sources = [str(base_path)]

    if env_path.exists():
        with open(env_path, "r") as f:
            env_overrides = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, env_overrides)
        sources.append(str(env_path))

    # Allow ad-hoc local overrides (gitignored) for engineer workstations.
    local_path = config_dir / "local.yaml"
    if local_path.exists():
        with open(local_path, "r") as f:
            local_overrides = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, local_overrides)
        sources.append(str(local_path))

    merged = _resolve_env_placeholders(merged)

    return PlatformConfig(
        raw=merged,
        environment=environment,
        loaded_at=datetime.now(timezone.utc),
        source_files=tuple(sources),
    )
