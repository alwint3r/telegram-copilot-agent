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
    """Final assistant reply payload returned from one ask call."""

    reply: str


@dataclass
class PendingUserInput:
    """Stores one outstanding user-input request awaiting a Telegram reply."""

    future: asyncio.Future[UserInputResponse]
    question: str
    choices: list[str]
    allow_freeform: bool


@dataclass(frozen=True)
class DispatcherConfig:
    """Runtime settings for background dispatch behavior."""

    user_input_timeout_seconds: int
    shutdown_drain_timeout_seconds: int


@dataclass(frozen=True)
class StartupConfig:
    """Process-level startup configuration resolved from environment values."""

    api_key: str
    github_token: str | None
    model: str
    timeout_seconds: int
    log_level: LogLevel
    reasoning_effort: str | None
    binary_download_max_bytes: int
    binary_download_timeout_seconds: int
    skill_tool_max_calls_per_ask: int
