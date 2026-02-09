"""Environment-driven runtime configuration."""

import logging
import os
from typing import cast

from copilot.types import LogLevel

from .constants import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    DEFAULT_MAX_ARTIFACTS_PER_REQUEST,
    DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
    DEFAULT_USER_INPUT_TIMEOUT_SECONDS,
    VALID_REASONING_EFFORTS,
)
from .models import DispatcherConfig, StartupConfig

logger = logging.getLogger(__name__)


def read_env_int(name: str, default: int) -> int:
    """Read an integer environment variable with fallback on invalid values."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        logger.warning(
            "Invalid integer for %s=%r. Using default %s.", name, raw_value, default
        )
        return default


def parse_log_level(value: str) -> LogLevel:
    """Normalize log level text into a valid Copilot SDK log level."""

    valid_log_levels = {"none", "error", "warning", "info", "debug", "all"}
    normalized_value = value.lower()
    normalized = normalized_value if normalized_value in valid_log_levels else "info"
    return cast(LogLevel, normalized)


def parse_reasoning_effort(value: str | None) -> str | None:
    """Normalize optional reasoning effort value with validation."""

    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in VALID_REASONING_EFFORTS:
        return normalized
    logger.warning(
        "Invalid COPILOT_REASONING_EFFORT=%r. Ignoring reasoning effort.",
        value,
    )
    return None


def model_supports_reasoning_effort(model: str) -> bool:
    """Return True for gpt-5+ model names that support reasoning effort."""

    normalized = model.strip().lower()
    if not normalized.startswith("gpt-"):
        return False

    version = normalized[4:]
    major_digits = []
    for char in version:
        if not char.isdigit():
            break
        major_digits.append(char)
    if not major_digits:
        return False
    return int("".join(major_digits)) >= 5


def is_reasoning_effort_unsupported_error(error: Exception) -> bool:
    """Return True when session creation failed due to unsupported reasoning effort."""

    message = str(error).lower()
    if "reasoning effort" not in message:
        return False
    return "does not support" in message or "unsupported" in message


def load_dispatcher_config() -> DispatcherConfig:
    """Load dispatcher-related settings from environment variables."""

    return DispatcherConfig(
        user_input_timeout_seconds=read_env_int(
            "COPILOT_USER_INPUT_TIMEOUT_SECONDS", DEFAULT_USER_INPUT_TIMEOUT_SECONDS
        ),
        max_artifacts_per_request=read_env_int(
            "TELEGRAM_MAX_ARTIFACTS", DEFAULT_MAX_ARTIFACTS_PER_REQUEST
        ),
        max_artifact_bytes=read_env_int(
            "TELEGRAM_MAX_ARTIFACT_BYTES", DEFAULT_MAX_ARTIFACT_BYTES
        ),
        shutdown_drain_timeout_seconds=read_env_int(
            "TELEGRAM_SHUTDOWN_DRAIN_TIMEOUT_SECONDS",
            DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
        ),
    )


def load_startup_config() -> StartupConfig:
    """Load startup configuration and validate required environment variables."""

    api_key = os.getenv("TELEGRAM_BOT_API_KEY")
    if not api_key:
        raise RuntimeError("TELEGRAM_BOT_API_KEY is not defined")

    return StartupConfig(
        api_key=api_key,
        model=os.getenv("COPILOT_MODEL", "gpt-5"),
        timeout_seconds=read_env_int("COPILOT_TIMEOUT_SECONDS", 600),
        log_level=parse_log_level(os.getenv("COPILOT_LOG_LEVEL", "info")),
        reasoning_effort=parse_reasoning_effort(os.getenv("COPILOT_REASONING_EFFORT")),
    )
