"""Dataclasses representing runtime state for the Telegram bot."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from copilot.types import LogLevel, UserInputResponse


@dataclass
class SessionState:
    """Tracks a live Copilot session and its per-session lock."""

    session: Any
    session_id: str
    lock: asyncio.Lock
    closed: bool = False


@dataclass
class PromptItem:
    """Represents one user prompt queued for background processing."""

    request_id: int
    text: str
    reply_to_message_id: int | None


@dataclass
class ChatRuntime:
    """Holds queue and worker state for one Telegram chat."""

    queue: asyncio.Queue[PromptItem] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    next_request_id: int = 1


@dataclass
class AskResult:
    """Final assistant reply plus discovered artifact file paths."""

    reply: str
    artifact_paths: list[str] = field(default_factory=list)


@dataclass
class PendingUserInput:
    """Stores one outstanding user-input request awaiting a Telegram reply."""

    future: asyncio.Future[UserInputResponse]
    question: str
    choices: list[str]
    allow_freeform: bool


@dataclass(frozen=True)
class DispatcherConfig:
    """Runtime settings for background dispatch and artifact sending."""

    user_input_timeout_seconds: int
    max_artifacts_per_request: int
    max_artifact_bytes: int
    shutdown_drain_timeout_seconds: int


@dataclass(frozen=True)
class StartupConfig:
    """Process-level startup configuration resolved from environment values."""

    api_key: str
    model: str
    timeout_seconds: int
    log_level: LogLevel
    reasoning_effort: str | None
