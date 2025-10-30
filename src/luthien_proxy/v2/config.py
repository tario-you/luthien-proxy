# ABOUTME: V2 configuration loading - policy instantiation from YAML
# ABOUTME: Loads policy class and config from YAML file specified by V2_POLICY_CONFIG

"""Configuration loading utilities for the V2 gateway.

This module provides helpers to load both:
- Policy configuration (class + kwargs)
- Runtime gateway settings (provider auth, gateway behavior)

The YAML schema extends the historical policy-only format with optional sections:

```yaml
gateway:
  allow_client_provider_keys: true

providers:
  openai:
    enabled: true
    api_key: "sk-..."
    org: "org_..."

policy:
  class: "luthien_proxy.v2.policies.noop:NoOpPolicy"
  config: {}
```
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, cast

import yaml

from luthien_proxy.v2.policies.base import LuthienPolicy
from luthien_proxy.v2.policies.noop import NoOpPolicy

logger = logging.getLogger(__name__)


DEFAULT_PROVIDER_ENV_VARS: dict[str, dict[str, str]] = {
    "openai": {"api_key": "OPENAI_API_KEY", "organization": "OPENAI_ORGANIZATION"},
    "anthropic": {"api_key": "ANTHROPIC_API_KEY", "organization": "ANTHROPIC_ORGANIZATION"},
}


@dataclass
class GatewaySettings:
    """Runtime gateway settings loaded from YAML."""

    allow_client_provider_keys: bool = False


@dataclass
class ProviderSettings:
    """Configuration for a single upstream provider."""

    name: str
    enabled: bool = True
    api_key: str | None = None
    org: str | None = None
    env_var: str | None = None
    org_env_var: str | None = None

    def __post_init__(self) -> None:
        defaults = DEFAULT_PROVIDER_ENV_VARS.get(self.name, {})
        if self.env_var is None:
            self.env_var = defaults.get("api_key")
        if self.org_env_var is None:
            self.org_env_var = defaults.get("organization")


@dataclass
class RuntimeConfig:
    """Aggregated runtime configuration for the gateway."""

    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    providers: dict[str, ProviderSettings] = field(default_factory=dict)

    def get_provider(self, name: str) -> ProviderSettings:
        """Return provider settings, creating defaults when missing."""
        if name not in self.providers:
            self.providers[name] = ProviderSettings(name=name)
        return self.providers[name]


def _resolve_config_path(config_path: str | None) -> str:
    """Determine the configuration file path."""
    if config_path is not None:
        return config_path
    env_path = os.getenv("V2_POLICY_CONFIG")
    if env_path:
        return env_path
    return "config/v2_config.yaml"


def _load_config_dict(config_path: str | None = None) -> dict[str, Any]:
    """Load YAML configuration into a dictionary."""
    path = _resolve_config_path(config_path)

    if not os.path.exists(path):
        logger.warning(f"Policy config not found at {path}; using defaults")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as file:
            cfg = yaml.safe_load(file) or {}
            if not isinstance(cfg, dict):
                logger.warning(f"Configuration at {path} is not a mapping; using defaults")
                return {}
            return cfg
    except Exception as exc:
        logger.error(f"Failed to read configuration {path}: {exc}")
        return {}


def load_policy_from_yaml(config_path: str | None = None) -> LuthienPolicy:
    """Load a policy from YAML configuration file.

    Args:
        config_path: Path to YAML config file. If None, uses V2_POLICY_CONFIG env var.
                    Defaults to config/v2_config.yaml if env var not set.

    Returns:
        Instantiated policy object

    Raises:
        RuntimeError: If config file cannot be read or policy cannot be loaded
    """
    cfg = _load_config_dict(config_path)
    if not cfg:
        return NoOpPolicy()

    # Extract policy section
    policy_section = cfg.get("policy")
    if not isinstance(policy_section, dict):
        logger.warning("No valid 'policy' section found; using NoOpPolicy")
        return NoOpPolicy()

    policy_class_ref = policy_section.get("class")
    policy_config = policy_section.get("config", {})

    if not policy_class_ref:
        logger.warning(f"No 'class' specified in policy section of {config_path}; using NoOpPolicy")
        return NoOpPolicy()

    # Import policy class
    try:
        policy_class = _import_policy_class(policy_class_ref)
    except Exception as exc:
        logger.error(f"Failed to import policy '{policy_class_ref}': {exc}")
        return NoOpPolicy()

    # Validate it's a LuthienPolicy subclass
    if not issubclass(policy_class, LuthienPolicy):
        logger.warning(f"Policy class {policy_class_ref} does not subclass LuthienPolicy; using NoOpPolicy")
        return NoOpPolicy()

    # Instantiate policy
    try:
        policy = _instantiate_policy(policy_class, policy_config)
        logger.info(f"Loaded policy from {config_path}: {policy_class.__name__}")
        return policy
    except Exception as exc:
        logger.error(f"Failed to instantiate policy {policy_class_ref} with config {policy_config}: {exc}")
        return NoOpPolicy()


def load_runtime_config(config_path: str | None = None) -> RuntimeConfig:
    """Load gateway runtime configuration (gateway + provider settings)."""
    cfg = _load_config_dict(config_path)
    runtime = RuntimeConfig()

    gateway_section = cfg.get("gateway")
    if isinstance(gateway_section, dict):
        runtime.gateway = GatewaySettings(
            allow_client_provider_keys=bool(gateway_section.get("allow_client_provider_keys", False)),
        )

    providers_section = cfg.get("providers")
    if isinstance(providers_section, dict):
        for name, raw_settings in providers_section.items():
            if isinstance(raw_settings, dict):
                provider = ProviderSettings(
                    name=name,
                    enabled=bool(raw_settings.get("enabled", True)),
                    api_key=raw_settings.get("api_key"),
                    org=raw_settings.get("org") or raw_settings.get("organization"),
                    env_var=raw_settings.get("env_var"),
                    org_env_var=raw_settings.get("org_env_var"),
                )
                runtime.providers[name] = provider
            elif isinstance(raw_settings, bool):
                runtime.providers[name] = ProviderSettings(name=name, enabled=raw_settings)
            else:
                logger.warning(f"Ignoring invalid provider settings for '{name}': {raw_settings}")

    return runtime


def _import_policy_class(class_ref: str) -> type[LuthienPolicy]:
    """Import a policy class from a module:class reference.

    Args:
        class_ref: String like "module.path:ClassName"

    Returns:
        Policy class

    Raises:
        ValueError: If class_ref format is invalid
        ImportError: If module cannot be imported
        AttributeError: If class doesn't exist in module
        TypeError: If the reference is not a class
    """
    if ":" not in class_ref:
        raise ValueError(f"Policy class reference must be in format 'module.path:ClassName', got: {class_ref}")

    module_path, class_name = class_ref.split(":", 1)

    # Import module
    module = __import__(module_path, fromlist=[class_name])

    # Get class from module
    cls = getattr(module, class_name)

    # Validate it's a class
    if not isinstance(cls, type):
        raise TypeError(f"{class_name} is not a class")

    return cast(type[LuthienPolicy], cls)


def _instantiate_policy(policy_class: type[LuthienPolicy], config: dict[str, Any]) -> LuthienPolicy:
    """Instantiate a policy with the given config.

    Args:
        policy_class: Policy class to instantiate
        config: Configuration dictionary (will be passed as **kwargs)

    Returns:
        Instantiated policy

    Raises:
        TypeError: If config parameters don't match policy constructor
    """
    if config:
        return policy_class(**config)
    else:
        return policy_class()


__all__ = [
    "GatewaySettings",
    "ProviderSettings",
    "RuntimeConfig",
    "load_policy_from_yaml",
    "load_runtime_config",
]
