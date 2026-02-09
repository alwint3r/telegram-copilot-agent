"""Telegram bot that proxies chat requests to Copilot sessions.

The module keeps one Copilot session per Telegram chat, executes prompts in a
background queue, streams progress updates, and sends generated artifacts back
to the user when possible.
"""

import argparse

import asyncio
from contextlib import suppress
import logging
import mimetypes
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from copilot import CopilotClient
from copilot.types import (
    CopilotClientOptions,
    LogLevel,
    PreToolUseHookInput,
    PreToolUseHookOutput,
    SessionConfig,
    UserInputRequest,
    UserInputResponse,
)
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULT_USER_INPUT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_ARTIFACTS_PER_REQUEST = 8
DEFAULT_MAX_ARTIFACT_BYTES = 45 * 1024 * 1024
DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 15
PROGRESS_EDIT_THROTTLE_SECONDS = 1.2
PROGRESS_PREVIEW_CHARS = 1200
VALID_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}

IGNORED_SCAN_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}
TEXTUAL_ARTIFACT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
    ".svg",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".sql",
    ".log",
}
DIRECT_SEND_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".zip",
}


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


@dataclass
class AskEventCollector:
    """Collects streaming Copilot events and normalizes output state."""

    workspace_root: str
    done: asyncio.Event = field(default_factory=asyncio.Event)
    updated: asyncio.Event = field(default_factory=asyncio.Event)
    chunks: list[str] = field(default_factory=list)
    final_content: str = ""
    pending_statuses: list[str] = field(default_factory=list)
    artifact_paths: set[str] = field(default_factory=set)
    terminal_error: str | None = None

    def collect_path(self, path_value: str | None) -> None:
        """Collect a normalized in-workspace artifact candidate path."""

        if not path_value:
            return
        normalized = normalize_workspace_path(path_value, self.workspace_root)
        if normalized:
            self.artifact_paths.add(normalized)

    def on_event(self, event: Any) -> None:
        """Handle one SDK stream event and update aggregate response state."""

        event_type = getattr(event.type, "value", str(event.type))
        data = getattr(event, "data", None)

        if event_type in {"assistant.message_delta", "assistant.reasoning_delta"}:
            self.chunks.append(getattr(data, "delta_content", "") or "")
            self.updated.set()
        elif event_type == "assistant.message":
            self.final_content = getattr(data, "content", "") or ""
            self.updated.set()
        elif event_type == "assistant.reasoning":
            self.updated.set()
        elif event_type == "session.error":
            detail = self._build_event_detail(event_type, data)
            self.terminal_error = detail
            self.pending_statuses.append(detail)
            self.done.set()
            self.updated.set()
        elif event_type == "session.idle":
            self.done.set()
            self.updated.set()
        else:
            detail = self._build_event_detail(event_type, data)
            self.pending_statuses.append(detail)
            self.updated.set()

        if data is None:
            return

        self.collect_path(getattr(data, "path", None))
        attachments = getattr(data, "attachments", None) or []
        for attachment in attachments:
            self.collect_path(getattr(attachment, "path", None))
            self.collect_path(getattr(attachment, "file_path", None))
        for payload in (
            getattr(data, "arguments", None),
            getattr(data, "input", None),
            getattr(data, "output", None),
        ):
            self.artifact_paths.update(
                extract_existing_paths_from_obj(payload, self.workspace_root)
            )

    @staticmethod
    def _build_event_detail(event_type: str, data: Any) -> str:
        """Build a readable detail string for status/error event payloads."""

        detail_value: Any = None
        if isinstance(data, dict):
            for key in ("progress_message", "message", "error"):
                value = data.get(key)
                if value:
                    detail_value = value
                    break
        else:
            for attr in ("progress_message", "message", "error"):
                value = getattr(data, attr, None)
                if value:
                    detail_value = value
                    break

        if detail_value is None:
            return event_type
        return f"{event_type}: {detail_value}"

    def snapshot(self) -> str:
        """Return the best current textual snapshot of assistant output."""

        return self.final_content.strip() or "".join(self.chunks).strip()

    def pop_pending_statuses(self) -> list[str]:
        """Drain and return queued status updates."""

        statuses = list(self.pending_statuses)
        self.pending_statuses.clear()
        return statuses

    def build_result(self) -> AskResult:
        """Build the final ask result from collected stream data."""

        reply = self.snapshot()
        if not reply:
            reply = "I could not generate a response."
        return AskResult(reply=reply, artifact_paths=sorted(self.artifact_paths))


@dataclass
class WorkerProgressReporter:
    """Handles throttled progress updates for one queued Telegram request."""

    bot: Any
    chat_id: int
    request_id: int
    message_id: int
    last_edit_text: str
    last_edit_time: float = 0.0
    done: asyncio.Event = field(default_factory=asyncio.Event)

    async def safe_edit(self, text: str) -> None:
        """Edit the progress message and ignore benign Telegram edit errors."""

        if text == self.last_edit_text:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
            self.last_edit_text = text
            self.last_edit_time = time.monotonic()
        except BadRequest as exc:
            message = str(exc).lower()
            if "message is not modified" not in message:
                logger.warning("Failed to edit progress message: %s", exc)
        except Exception:
            logger.exception("Unexpected error while editing progress message")

    async def progress_callback(self, kind: str, payload: str) -> None:
        """Bridge Copilot streaming updates into Telegram progress text."""

        if kind == "event":
            if "tool" in payload or "approval" in payload:
                await self.safe_edit(
                    f"Request {self.request_id}: still working.\nStatus: {payload}"
                )
            return

        if kind == "partial":
            now = time.monotonic()
            if now - self.last_edit_time < PROGRESS_EDIT_THROTTLE_SECONDS:
                return
            preview = payload[-PROGRESS_PREVIEW_CHARS:].strip()
            if preview:
                await self.safe_edit(
                    f"Request {self.request_id}: generating response...\n\n{preview}"
                )

    async def heartbeat(self) -> None:
        """Post periodic progress updates while a request remains active."""

        started = time.monotonic()
        while not self.done.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if self.done.is_set():
                return
            elapsed = int(time.monotonic() - started)
            await self.safe_edit(
                f"Request {self.request_id}: still working in the background ({elapsed}s)."
            )


def split_for_telegram(text: str, chunk_size: int = 4096) -> list[str]:
    """Split a message into Telegram-safe chunks, preferring newline breaks."""

    if len(text) <= chunk_size:
        return [text]

    parts: list[str] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        if end < text_length:
            newline_pos = text.rfind("\n", start, end)
            if newline_pos > start:
                end = newline_pos + 1

        chunk = text[start:end]
        if not chunk:
            # Defensive fallback to avoid stalling if boundary math ever regresses.
            end = min(start + chunk_size, text_length)
            chunk = text[start:end]
        parts.append(chunk)
        start = end
    return parts


def normalize_workspace_path(value: str, workspace_root: str) -> str | None:
    """Normalize a path and keep it inside the workspace boundary."""

    if not value:
        return None

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(workspace_root) / candidate
    resolved = candidate.resolve(strict=False)
    root_resolved = Path(workspace_root).resolve(strict=False)

    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None

    return str(resolved)


def extract_existing_paths_from_obj(
    payload: Any,
    workspace_root: str,
    depth: int = 0,
) -> set[str]:
    """Recursively collect existing file paths from nested payload objects."""

    if payload is None or depth > 4:
        return set()

    paths: set[str] = set()
    if isinstance(payload, dict):
        for value in payload.values():
            paths.update(
                extract_existing_paths_from_obj(value, workspace_root, depth + 1)
            )
        return paths

    if isinstance(payload, (list, tuple)):
        for value in payload:
            paths.update(
                extract_existing_paths_from_obj(value, workspace_root, depth + 1)
            )
        return paths

    if isinstance(payload, str):
        maybe_path = normalize_workspace_path(payload.strip(), workspace_root)
        if maybe_path and os.path.isfile(maybe_path):
            paths.add(maybe_path)
    return paths


def snapshot_workspace_files(workspace_root: str) -> dict[str, tuple[float, int]]:
    """Capture file modification metadata for a workspace snapshot."""

    snapshot: dict[str, tuple[float, int]] = {}
    for current_root, dirs, files in os.walk(workspace_root):
        dirs[:] = [name for name in dirs if name not in IGNORED_SCAN_DIRS]
        for file_name in files:
            path = os.path.join(current_root, file_name)
            try:
                stat_result = os.stat(path)
            except OSError:
                continue
            snapshot[path] = (stat_result.st_mtime, stat_result.st_size)
    return snapshot


def find_new_or_modified_files(
    before_snapshot: dict[str, tuple[float, int]],
    workspace_root: str,
) -> list[str]:
    """Return workspace files that are new or changed from a prior snapshot."""

    candidates: list[str] = []
    for current_root, dirs, files in os.walk(workspace_root):
        dirs[:] = [name for name in dirs if name not in IGNORED_SCAN_DIRS]
        for file_name in files:
            path = os.path.join(current_root, file_name)
            try:
                stat_result = os.stat(path)
            except OSError:
                continue
            previous = before_snapshot.get(path)
            if previous is None:
                candidates.append(path)
                continue
            if (
                previous[0] != stat_result.st_mtime
                or previous[1] != stat_result.st_size
            ):
                candidates.append(path)
    return candidates


def is_sendable_artifact(path: str, max_artifact_bytes: int) -> bool:
    """Return True when a file should be sent back to Telegram as an artifact."""

    if not os.path.isfile(path):
        return False
    extension = Path(path).suffix.lower()
    if extension in TEXTUAL_ARTIFACT_EXTENSIONS:
        return False
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return False
    if size_bytes <= 0 or size_bytes > max_artifact_bytes:
        return False
    if extension in DIRECT_SEND_EXTENSIONS:
        return True
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        return False
    if mime_type.startswith("text/"):
        return False
    return mime_type.startswith(("image/", "audio/", "video/")) or mime_type in {
        "application/pdf",
        "application/zip",
    }


def default_user_input_answer(
    choices: list[str], allow_freeform: bool
) -> UserInputResponse:
    """Pick a safe default answer for unattended Copilot input requests."""

    if choices:
        lowered = {choice.lower(): choice for choice in choices}
        for key in ("no", "cancel", "deny", "abort", "skip"):
            if key in lowered:
                return {"answer": lowered[key], "wasFreeform": False}
        return {"answer": choices[0], "wasFreeform": False}

    default_text = (
        os.getenv("COPILOT_USER_INPUT_DEFAULT_ANSWER")
        or "Proceed using your best judgment and continue."
    )
    return {"answer": default_text, "wasFreeform": allow_freeform}


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


class CopilotSessionManager:
    """Owns one Copilot session per chat and serializes access to each session."""

    def __init__(
        self,
        client: CopilotClient,
        model: str,
        timeout_seconds: int,
        reasoning_effort: str | None = None,
        working_directory: str | None = None,
    ) -> None:
        """Initialize the manager with client and default session settings."""

        self._client = client
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._reasoning_effort = reasoning_effort
        self._sessions: dict[int, SessionState] = {}
        self._session_to_chat: dict[str, int] = {}
        self._sessions_lock = asyncio.Lock()
        self._user_input_requester: (
            Callable[[int, UserInputRequest], Awaitable[UserInputResponse]] | None
        ) = None
        self._working_directory = (
            working_directory if working_directory is not None else os.getcwd()
        )

    def set_user_input_requester(
        self, requester: Callable[[int, UserInputRequest], Awaitable[UserInputResponse]]
    ) -> None:
        """Set callback used when Copilot asks for interactive confirmation."""

        self._user_input_requester = requester

    async def _get_or_create_session(self, chat_id: int) -> SessionState:
        """Return an active session for a chat, creating one when needed."""

        existing_state = self._sessions.get(chat_id)
        if existing_state is not None and not existing_state.closed:
            return existing_state

        async with self._sessions_lock:
            existing_state = self._sessions.get(chat_id)
            if existing_state is not None and not existing_state.closed:
                return existing_state

            session_config: dict[str, Any] = {
                "model": self._model,
                "working_directory": self._working_directory,
                "streaming": True,
                "on_user_input_request": self._on_user_input_request,
                "hooks": {
                    "on_pre_tool_use": self._on_pre_tool_use,
                },
            }
            apply_reasoning_effort = bool(
                self._reasoning_effort and model_supports_reasoning_effort(self._model)
            )
            if apply_reasoning_effort:
                session_config["reasoning_effort"] = self._reasoning_effort
            try:
                session = await self._client.create_session(
                    cast(SessionConfig, session_config)
                )
            except Exception as exc:
                if (
                    not apply_reasoning_effort
                    or not is_reasoning_effort_unsupported_error(exc)
                ):
                    raise

                logger.warning(
                    "Model %r rejected reasoning effort %r. Retrying without reasoning effort.",
                    self._model,
                    self._reasoning_effort,
                )
                session_config.pop("reasoning_effort", None)
                session = await self._client.create_session(
                    cast(SessionConfig, session_config)
                )
            session_id = getattr(session, "session_id", "")
            new_state = SessionState(
                session=session, session_id=session_id, lock=asyncio.Lock()
            )
            self._sessions[chat_id] = new_state
            if session_id:
                self._session_to_chat[session_id] = chat_id
            return new_state

    async def _on_pre_tool_use(
        self, input_data: PreToolUseHookInput, _invocation: dict[str, str]
    ) -> PreToolUseHookOutput:
        """Allow tool execution and provide extra context for long-running tasks."""

        tool_name = input_data.get("toolName", "unknown")
        logger.info("Allowing tool call: %s", tool_name)
        return {
            "permissionDecision": "allow",
            "modifiedArgs": input_data.get("toolArgs"),
            "additionalContext": "This bot runs long tasks in background mode. Continue execution.",
        }

    def _resolve_chat_id_for_invocation(self, invocation: Any) -> int | None:
        """Map an SDK invocation payload to its originating chat id."""

        session_id = None
        if isinstance(invocation, dict):
            session_id = invocation.get("session_id")
        if not session_id:
            return None
        return self._session_to_chat.get(session_id)

    async def _on_user_input_request(
        self, request: UserInputRequest, invocation: dict[str, str]
    ) -> UserInputResponse:
        """Handle SDK user-input requests via Telegram or safe defaults."""

        question = request.get("question", "No question provided")
        chat_id = self._resolve_chat_id_for_invocation(invocation)
        logger.info("User input requested for chat_id=%s: %s", chat_id, question)

        if chat_id is not None and self._user_input_requester is not None:
            return await self._user_input_requester(chat_id, request)

        return default_user_input_answer(
            choices=request.get("choices") or [],
            allow_freeform=bool(request.get("allowFreeform", True)),
        )

    async def _wait_for_session_idle(
        self,
        collector: AskEventCollector,
        progress_callback: Callable[[str, str], Awaitable[None]] | None,
        deadline: float | None = None,
    ) -> None:
        """Wait for idle while forwarding status/partial updates to Telegram."""

        last_partial_length = 0
        if deadline is None:
            deadline = time.monotonic() + self._timeout_seconds

        while not collector.done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError

            try:
                await asyncio.wait_for(
                    collector.updated.wait(), timeout=min(1.0, remaining)
                )
            except asyncio.TimeoutError:
                continue

            collector.updated.clear()
            if progress_callback is None:
                continue

            for status in collector.pop_pending_statuses():
                await progress_callback("event", status)

            snapshot = collector.snapshot()
            if len(snapshot) > last_partial_length:
                last_partial_length = len(snapshot)
                await progress_callback("partial", snapshot)

        if progress_callback is not None:
            for status in collector.pop_pending_statuses():
                await progress_callback("event", status)
        if collector.terminal_error is not None:
            raise RuntimeError(f"Copilot stream failed: {collector.terminal_error}")

    async def _stream_session_response(
        self,
        state: SessionState,
        prompt: str,
        progress_callback: Callable[[str, str], Awaitable[None]] | None,
    ) -> AskResult:
        """Send one prompt and collect its full streamed response."""

        collector = AskEventCollector(workspace_root=os.getcwd())
        unsubscribe = state.session.on(collector.on_event)
        deadline = time.monotonic() + self._timeout_seconds
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(
                state.session.send({"prompt": prompt}),
                timeout=remaining,
            )
            await self._wait_for_session_idle(
                collector,
                progress_callback,
                deadline=deadline,
            )
        finally:
            unsubscribe()

        return collector.build_result()

    async def ask(
        self,
        chat_id: int,
        prompt: str,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> AskResult:
        """Send a prompt through the chat session and return its final response."""

        while True:
            state = await self._get_or_create_session(chat_id)

            async with state.lock:
                if state.closed:
                    logger.info(
                        "Skipping closed session for chat_id=%s session_id=%s",
                        chat_id,
                        state.session_id,
                    )
                    continue

                return await self._stream_session_response(
                    state=state,
                    prompt=prompt,
                    progress_callback=progress_callback,
                )

    async def reset_session(self, chat_id: int) -> bool:
        """Reset and destroy the active session for one chat if it exists."""

        async with self._sessions_lock:
            state = self._sessions.pop(chat_id, None)
            if state is None:
                return False
            state.closed = True

        session_id = state.session_id
        started = time.monotonic()
        destroy_succeeded = True

        try:
            async with state.lock:
                await state.session.destroy()
        except Exception:
            destroy_succeeded = False
            logger.exception(
                "Failed to destroy session during reset chat_id=%s session_id=%s",
                chat_id,
                session_id,
            )
        finally:
            if session_id:
                async with self._sessions_lock:
                    mapped_chat = self._session_to_chat.get(session_id)
                    if mapped_chat == chat_id:
                        self._session_to_chat.pop(session_id, None)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "Reset completed for chat_id=%s session_id=%s destroy_succeeded=%s elapsed_ms=%s",
            chat_id,
            session_id,
            destroy_succeeded,
            elapsed_ms,
        )
        return True

    async def shutdown(self) -> None:
        """Destroy all remaining sessions during process shutdown."""

        async with self._sessions_lock:
            sessions = list(self._sessions.values())
            for state in sessions:
                state.closed = True
            self._sessions.clear()
            self._session_to_chat.clear()

        for state in sessions:
            try:
                await state.session.destroy()
            except Exception:
                logger.exception("Failed to destroy Copilot session")


class BackgroundDispatcher:
    """Queues prompts per chat and executes them in background workers."""

    def __init__(
        self,
        application: Application,
        manager: CopilotSessionManager,
    ) -> None:
        """Initialize dispatcher runtime state for all Telegram chats."""

        self._application = application
        self._manager = manager
        self._config = load_dispatcher_config()
        self._chat_runtimes: dict[int, ChatRuntime] = {}
        self._pending_user_inputs: dict[int, PendingUserInput] = {}
        self._lock = asyncio.Lock()
        self._shutdown_requested = threading.Event()
        self._force_shutdown_requested = threading.Event()

    @property
    def is_shutting_down(self) -> bool:
        """Return whether graceful shutdown has been requested."""

        return self._shutdown_requested.is_set()

    @property
    def shutdown_drain_timeout_seconds(self) -> int:
        """Return the configured graceful drain timeout in seconds."""

        return self._config.shutdown_drain_timeout_seconds

    def request_shutdown(self, *, force: bool = False) -> bool:
        """Mark shutdown as requested and optionally escalate to force-cancel mode."""

        first_request = not self._shutdown_requested.is_set()
        self._shutdown_requested.set()
        if force:
            self._force_shutdown_requested.set()
        return first_request

    async def enqueue(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None,
    ) -> int | None:
        """Queue a prompt for a chat and ensure a worker is running."""

        if self._shutdown_requested.is_set():
            logger.info("Rejecting enqueue during shutdown for chat_id=%s", chat_id)
            return None

        async with self._lock:
            if self._shutdown_requested.is_set():
                logger.info("Rejecting enqueue during shutdown for chat_id=%s", chat_id)
                return None

            runtime = self._chat_runtimes.get(chat_id)
            if runtime is None:
                runtime = ChatRuntime()
                self._chat_runtimes[chat_id] = runtime

            request_id = runtime.next_request_id
            runtime.next_request_id += 1
            await runtime.queue.put(
                PromptItem(
                    request_id=request_id,
                    text=text,
                    reply_to_message_id=reply_to_message_id,
                )
            )

            if runtime.worker is None or runtime.worker.done():
                runtime.worker = asyncio.create_task(
                    self._chat_worker(chat_id, runtime)
                )

            return runtime.queue.qsize()

    @staticmethod
    def _resolve_selected_choice(candidate: str, choices: list[str]) -> str | None:
        """Resolve numeric or case-insensitive user selection to a choice string."""

        if candidate.isdigit():
            index = int(candidate)
            if 1 <= index <= len(choices):
                return choices[index - 1]

        for choice in choices:
            if candidate.casefold() == choice.casefold():
                return choice
        return None

    def _format_user_input_prompt(
        self,
        question: str,
        choices: list[str],
        allow_freeform: bool,
    ) -> str:
        """Render a user-input request into plain Telegram text."""

        lines = ["Copilot needs your confirmation/input:", question]
        if choices:
            lines.append("")
            lines.append("Reply with one of these options:")
            for index, choice in enumerate(choices, start=1):
                lines.append(f"{index}. {choice}")
        if allow_freeform:
            lines.append("")
            lines.append("You can also reply with free-form text.")
        return "\n".join(lines)

    async def request_user_input(
        self,
        chat_id: int,
        request: UserInputRequest,
    ) -> UserInputResponse:
        """Ask Telegram user for input required by Copilot and await response."""

        question = request.get("question", "Please confirm how I should continue.")
        choices = list(request.get("choices") or [])
        allow_freeform = bool(request.get("allowFreeform", True))
        future: asyncio.Future[UserInputResponse] = (
            asyncio.get_running_loop().create_future()
        )

        previous: PendingUserInput | None = None
        async with self._lock:
            previous = self._pending_user_inputs.get(chat_id)
            self._pending_user_inputs[chat_id] = PendingUserInput(
                future=future,
                question=question,
                choices=choices,
                allow_freeform=allow_freeform,
            )
        if previous and not previous.future.done():
            previous.future.set_result(
                default_user_input_answer(previous.choices, previous.allow_freeform)
            )

        try:
            await self._application.bot.send_message(
                chat_id=chat_id,
                text=self._format_user_input_prompt(question, choices, allow_freeform),
            )
            return await asyncio.wait_for(
                future, timeout=self._config.user_input_timeout_seconds
            )
        except asyncio.TimeoutError:
            fallback = default_user_input_answer(choices, allow_freeform)
            await self._application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "No response received in time. "
                    f'Continuing with: "{fallback["answer"]}".'
                ),
            )
            return fallback
        finally:
            async with self._lock:
                current = self._pending_user_inputs.get(chat_id)
                if current and current.future is future:
                    self._pending_user_inputs.pop(chat_id, None)

    async def consume_user_input_reply(
        self, chat_id: int, text: str
    ) -> tuple[bool, str | None]:
        """Consume a user reply if a Copilot input prompt is currently pending."""

        async with self._lock:
            pending = self._pending_user_inputs.get(chat_id)

        if pending is None:
            return False, None

        candidate = text.strip()
        if not candidate:
            return True, "Please reply with a value so Copilot can continue."

        answer = candidate
        was_freeform = True
        if pending.choices:
            selected_choice = self._resolve_selected_choice(candidate, pending.choices)

            if selected_choice is not None:
                answer = selected_choice
                was_freeform = False
            elif not pending.allow_freeform:
                choices_string = ", ".join(pending.choices)
                return True, f"Invalid option. Reply with one of: {choices_string}"

        result: UserInputResponse = {"answer": answer, "wasFreeform": was_freeform}
        if not pending.future.done():
            pending.future.set_result(result)
        return True, "Response received. Continuing now."

    async def _send_artifacts(
        self,
        bot: Any,
        chat_id: int,
        artifact_candidates: list[str],
        reply_to_message_id: int | None,
    ) -> None:
        """Send eligible generated artifacts back to Telegram chat."""

        sent = 0
        sent_any = False
        seen: set[str] = set()
        workspace_root = Path(os.getcwd()).resolve(strict=False)
        for artifact_path in artifact_candidates:
            normalized = os.path.realpath(artifact_path)

            try:
                Path(normalized).relative_to(workspace_root)
            except ValueError:
                logger.warning("Skipping artifact outside workspace: %s", normalized)
                continue

            if normalized in seen:
                continue
            seen.add(normalized)

            if not is_sendable_artifact(normalized, self._config.max_artifact_bytes):
                continue
            if sent >= self._config.max_artifacts_per_request:
                break

            extension = Path(normalized).suffix.lower()
            mime_type, _ = mimetypes.guess_type(normalized)
            caption = f"Artifact: {os.path.basename(normalized)}"

            try:
                if extension in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".bmp",
                    ".tiff",
                } or (mime_type and mime_type.startswith("image/")):
                    with open(normalized, "rb") as file_obj:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=file_obj,
                            caption=caption,
                            reply_to_message_id=reply_to_message_id,
                            allow_sending_without_reply=True,
                        )
                else:
                    with open(normalized, "rb") as file_obj:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=file_obj,
                            caption=caption,
                            reply_to_message_id=reply_to_message_id,
                            allow_sending_without_reply=True,
                        )
                sent += 1
                sent_any = True
            except Exception:
                logger.exception("Failed to send artifact %s", normalized)

        if sent_any:
            await bot.send_message(
                chat_id=chat_id,
                text=f"Sent {sent} artifact(s).",
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )

    def _mark_runtime_worker_stopped(self, chat_id: int, runtime: ChatRuntime) -> None:
        """Clear worker reference while the dispatcher lock is already held."""

        current = self._chat_runtimes.get(chat_id)
        if current is runtime:
            runtime.worker = None

    async def _resolve_pending_user_inputs_for_shutdown(self) -> None:
        """Unblock pending user-input futures so workers can finish during shutdown."""

        async with self._lock:
            pending_inputs = list(self._pending_user_inputs.values())
            self._pending_user_inputs.clear()

        for pending in pending_inputs:
            if pending.future.done():
                continue
            pending.future.set_result(
                default_user_input_answer(pending.choices, pending.allow_freeform)
            )

    async def _create_progress_reporter(
        self,
        bot: Any,
        chat_id: int,
        item: PromptItem,
    ) -> WorkerProgressReporter:
        """Create and return a progress reporter for one queued request."""

        progress_message = await bot.send_message(
            chat_id=chat_id,
            text=f"Request {item.request_id}: started. I will update progress here.",
            reply_to_message_id=item.reply_to_message_id,
            allow_sending_without_reply=True,
        )
        return WorkerProgressReporter(
            bot=bot,
            chat_id=chat_id,
            request_id=item.request_id,
            message_id=progress_message.message_id,
            last_edit_text=progress_message.text or "",
        )

    async def _send_reply_chunks(
        self,
        bot: Any,
        chat_id: int,
        reply_to_message_id: int | None,
        reply: str,
    ) -> None:
        """Send assistant text reply as one or more Telegram-sized chunks."""

        for chunk in split_for_telegram(reply):
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )

    async def _safe_send_worker_message(
        self,
        bot: Any,
        chat_id: int,
        item: PromptItem,
        text: str,
    ) -> None:
        """Best-effort status send for worker error paths."""

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=item.reply_to_message_id,
                allow_sending_without_reply=True,
            )
        except Exception:
            logger.exception(
                "Failed to send worker status for chat_id=%s request_id=%s",
                chat_id,
                item.request_id,
            )

    @staticmethod
    def _collect_artifact_candidates(ask_result: AskResult) -> list[str]:
        """Return only request-scoped artifact paths reported by Copilot."""

        return list(ask_result.artifact_paths)

    async def _chat_worker(self, chat_id: int, runtime: ChatRuntime) -> None:
        """Process queued prompts for a single chat in FIFO order."""

        bot = self._application.bot
        while True:
            async with self._lock:
                current = self._chat_runtimes.get(chat_id)
                if current is not runtime:
                    return
                try:
                    item = runtime.queue.get_nowait()
                except asyncio.QueueEmpty:
                    self._mark_runtime_worker_stopped(chat_id, runtime)
                    return

            reporter: WorkerProgressReporter | None = None
            heartbeat_task: asyncio.Task | None = None
            try:
                reporter = await self._create_progress_reporter(bot, chat_id, item)
                heartbeat_task = asyncio.create_task(reporter.heartbeat())
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                ask_result = await self._manager.ask(
                    chat_id=chat_id,
                    prompt=item.text,
                    progress_callback=reporter.progress_callback,
                )
                await reporter.safe_edit(
                    f"Request {item.request_id}: done. Sending result now."
                )
                await self._send_reply_chunks(
                    bot=bot,
                    chat_id=chat_id,
                    reply_to_message_id=item.reply_to_message_id,
                    reply=ask_result.reply,
                )

                artifact_candidates = self._collect_artifact_candidates(
                    ask_result=ask_result,
                )
                await self._send_artifacts(
                    bot=bot,
                    chat_id=chat_id,
                    artifact_candidates=artifact_candidates,
                    reply_to_message_id=item.reply_to_message_id,
                )
            except asyncio.TimeoutError:
                logger.warning("Copilot response timed out for chat_id=%s", chat_id)
                if reporter is not None:
                    await reporter.safe_edit(f"Request {item.request_id}: timed out.")
                await self._safe_send_worker_message(
                    bot=bot,
                    chat_id=chat_id,
                    text="Request timed out. Please try again.",
                    item=item,
                )
            except Exception:
                logger.exception("Failed to process message for chat_id=%s", chat_id)
                if reporter is not None:
                    await reporter.safe_edit(f"Request {item.request_id}: failed.")
                await self._safe_send_worker_message(
                    bot=bot,
                    chat_id=chat_id,
                    text="I hit an error while contacting Copilot.",
                    item=item,
                )
            finally:
                if reporter is not None:
                    reporter.done.set()
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat_task
                runtime.queue.task_done()

    async def shutdown(self) -> None:
        """Drain queued and in-flight chat work before forcing cancellation."""

        self.request_shutdown()
        await self._resolve_pending_user_inputs_for_shutdown()

        async with self._lock:
            runtimes = list(self._chat_runtimes.values())

        all_tasks = [
            runtime.worker
            for runtime in runtimes
            if runtime.worker is not None and not runtime.worker.done()
        ]
        pending_tasks = list(all_tasks)

        timeout_seconds = self._config.shutdown_drain_timeout_seconds
        if self._force_shutdown_requested.is_set():
            timeout_seconds = 0

        if pending_tasks and timeout_seconds > 0:
            logger.info(
                "Graceful shutdown started. Waiting up to %ss for %s chat worker(s).",
                timeout_seconds,
                len(pending_tasks),
            )
            deadline = time.monotonic() + timeout_seconds
            while pending_tasks:
                if self._force_shutdown_requested.is_set():
                    logger.warning(
                        "Forced shutdown requested. Cancelling %s remaining worker(s).",
                        len(pending_tasks),
                    )
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Graceful shutdown timed out after %ss. Cancelling %s remaining worker(s).",
                        timeout_seconds,
                        len(pending_tasks),
                    )
                    break

                _, still_pending = await asyncio.wait(
                    pending_tasks,
                    timeout=min(1.0, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                pending_tasks = list(still_pending)

        for task in pending_tasks:
            task.cancel()

        for task in all_tasks:
            with suppress(asyncio.CancelledError):
                await task

        async with self._lock:
            self._chat_runtimes.clear()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/start` and explain how to use the bot."""

    if update.message:
        await update.message.reply_text("Send me a message and I will ask Copilot.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/reset` by clearing the active Copilot session for this chat."""

    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    manager: CopilotSessionManager | None = context.application.bot_data.get(
        "copilot_manager"
    )
    if manager is None:
        await message.reply_text(
            "Bot is still initializing. Please try again in a moment."
        )
        return

    await message.reply_text(
        "Reset requested. Waiting for any active Copilot request to finish...",
        allow_sending_without_reply=True,
    )

    reset_applied = await manager.reset_session(chat.id)
    if reset_applied:
        await message.reply_text(
            "Conversation history reset. Your next message starts a fresh Copilot session.",
            allow_sending_without_reply=True,
        )
        return

    await message.reply_text(
        "No active Copilot session was found. Your next message will still start fresh.",
        allow_sending_without_reply=True,
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inbound chat messages and enqueue them for background execution."""

    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or message.text is None:
        return

    dispatcher: BackgroundDispatcher | None = context.application.bot_data.get(
        "background_dispatcher"
    )
    if dispatcher is None:
        await message.reply_text(
            "Bot is still initializing. Please try again in a moment."
        )
        return

    consumed, reply_text = await dispatcher.consume_user_input_reply(
        chat_id=chat.id, text=message.text
    )
    if consumed:
        if reply_text:
            await message.reply_text(reply_text, allow_sending_without_reply=True)
        return

    pending = await dispatcher.enqueue(
        chat_id=chat.id,
        text=message.text,
        reply_to_message_id=message.message_id,
    )
    if pending is None:
        await message.reply_text(
            "Shutdown in progress. I am no longer accepting new requests.",
            allow_sending_without_reply=True,
        )
        return

    if pending > 1:
        await message.reply_text(
            f"Request queued ({pending} pending). I will process it in the background."
        )
    else:
        await message.reply_text(
            "Request received. I will process it in the background."
        )


async def on_startup(application: Application) -> None:
    """Start the Copilot SDK client once the Telegram app is ready."""

    client: CopilotClient = application.bot_data["copilot_client"]
    await client.start()


async def on_shutdown(application: Application) -> None:
    """Shutdown workers, sessions, and the Copilot SDK client."""

    dispatcher: BackgroundDispatcher = application.bot_data["background_dispatcher"]
    manager: CopilotSessionManager = application.bot_data["copilot_manager"]
    client: CopilotClient = application.bot_data["copilot_client"]
    await dispatcher.shutdown()
    await manager.shutdown()
    await client.stop()


def install_shutdown_signal_handlers(
    app: Application,
    dispatcher: BackgroundDispatcher,
) -> dict[int, Any]:
    """Install SIGINT/SIGTERM handlers that trigger graceful then forced shutdown."""

    previous_handlers: dict[int, Any] = {}

    def _handle_signal(signum: int, _frame: Any) -> None:
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = str(signum)

        first_signal = dispatcher.request_shutdown(force=False)
        if first_signal:
            logger.info(
                "Received %s. Starting graceful shutdown (drain timeout=%ss).",
                signal_name,
                dispatcher.shutdown_drain_timeout_seconds,
            )
        else:
            dispatcher.request_shutdown(force=True)
            logger.warning(
                "Received %s again. Escalating to forced shutdown.",
                signal_name,
            )

        with suppress(Exception):
            app.stop_running()

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)

    for signum in signals:
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_signal)

    return previous_handlers


def restore_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    """Restore previously registered OS signal handlers."""

    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)


def build_arg_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--working-directory",
        "-w",
        help="Working directory.",
        default=os.getcwd(),
        required=True,
    )
    parser.add_argument("--skills-directory", "-s", help="Skills directory")
    return parser


def main() -> None:
    """Build and run the Telegram polling application."""

    arg_parser = build_arg_parser()
    args = arg_parser.parse_args()
    startup_config = load_startup_config()

    client_options: CopilotClientOptions = {"log_level": startup_config.log_level}
    client = CopilotClient(client_options)
    manager = CopilotSessionManager(
        client=client,
        model=startup_config.model,
        timeout_seconds=startup_config.timeout_seconds,
        reasoning_effort=startup_config.reasoning_effort,
        working_directory=args.working_directory,
    )

    app = (
        Application.builder()
        .token(startup_config.api_key)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.bot_data["copilot_client"] = client
    app.bot_data["copilot_manager"] = manager
    dispatcher = BackgroundDispatcher(app, manager)
    manager.set_user_input_requester(dispatcher.request_user_input)
    app.bot_data["background_dispatcher"] = dispatcher

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))

    previous_signal_handlers = install_shutdown_signal_handlers(app, dispatcher)
    try:
        app.run_polling(stop_signals=None)
    finally:
        restore_signal_handlers(previous_signal_handlers)


if __name__ == "__main__":
    main()
