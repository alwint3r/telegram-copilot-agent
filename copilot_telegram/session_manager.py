"""Copilot session lifecycle and streaming response collection."""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, cast

from copilot import CopilotClient
from copilot.types import (
    PreToolUseHookInput,
    PreToolUseHookOutput,
    SessionConfig,
    UserInputRequest,
    UserInputResponse,
)

from .artifacts import (
    extract_existing_paths_from_obj,
    normalize_workspace_path,
    resolve_existing_path,
)
from .config import (
    is_reasoning_effort_unsupported_error,
    model_supports_reasoning_effort,
)
from .custom_tools import build_custom_tools
from .constants import (
    DEFAULT_ARTIFACT_REQUIRE_EXPLICIT_INTENT,
    DEFAULT_MAX_ARTIFACT_BYTES,
    DEFAULT_BINARY_DOWNLOAD_MAX_BYTES,
    DEFAULT_BINARY_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_SKILL_TOOL_MAX_CALLS_PER_ASK,
)
from .models import ArtifactIntent, AskResult, SessionState
from .user_input import default_user_input_answer

logger = logging.getLogger(__name__)


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
    external_artifact_paths: set[str] = field(default_factory=set)
    artifact_intents_by_path: dict[str, ArtifactIntent] = field(default_factory=dict)
    require_explicit_artifact_intent: bool = True
    terminal_error: str | None = None

    def collect_path(self, path_value: str | None) -> None:
        """Collect workspace artifact candidates and existing external files."""

        if not path_value:
            return
        normalized = normalize_workspace_path(path_value, self.workspace_root)
        if normalized:
            self.artifact_paths.add(normalized)
            return

        external = resolve_existing_path(path_value, self.workspace_root)
        if external:
            self.external_artifact_paths.add(external)

    def on_event(self, event: Any) -> None:
        """Handle one SDK stream event and update aggregate response state."""

        event_type = getattr(event.type, "value", str(event.type))
        data = getattr(event, "data", None)

        if event_type.startswith("tool."):
            if event_type == "tool.execution_complete":
                logger.info(
                    f"event: {event_type}, tool_call_id: {event.data.tool_call_id} result: {event.data.result.content}"
                )
            elif event_type == "tool.execution_start":
                logger.info(
                    f"evnet: {event_type}, tool_call_id: {event.data.tool_call_id}, tool_name: {event.data.tool_name}, arguments: {event.data.arguments}"
                )
            elif event_type == "tool.execution_partial_result":
                logger.info(
                    f"event: {event_type}, tool_call_id: {event.data.tool_call_id}, result: {event.data.partial_output}"
                )
            else:
                logger.info(f"event: {event.type} data: {event.data}")

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

        for intent in self._extract_explicit_artifact_intents(data):
            self._collect_artifact_intent(intent)

        if self.require_explicit_artifact_intent:
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
            for path in extract_existing_paths_from_obj(
                payload,
                self.workspace_root,
                allow_outside_workspace=True,
            ):
                normalized = normalize_workspace_path(path, self.workspace_root)
                if normalized:
                    self.artifact_paths.add(normalized)
                else:
                    self.external_artifact_paths.add(path)

    def _collect_artifact_intent(self, intent: ArtifactIntent) -> None:
        """Record one explicit artifact intent with best-effort caption merge."""

        normalized = normalize_workspace_path(intent.path, self.workspace_root)
        if normalized:
            path = normalized
            self.artifact_paths.add(path)
        else:
            resolved = resolve_existing_path(intent.path, self.workspace_root)
            if not resolved:
                return
            path = resolved
            self.external_artifact_paths.add(path)

        caption = intent.caption.strip() if intent.caption else None
        existing = self.artifact_intents_by_path.get(path)
        if existing is None:
            self.artifact_intents_by_path[path] = ArtifactIntent(
                path=path,
                caption=caption,
            )
            return
        if existing.caption is None and caption:
            self.artifact_intents_by_path[path] = ArtifactIntent(
                path=path,
                caption=caption,
            )

    def _extract_explicit_artifact_intents(
        self,
        payload: Any,
        depth: int = 0,
    ) -> list[ArtifactIntent]:
        """Extract explicit delivery intents from nested payload objects."""

        if payload is None or depth > 4:
            return []

        intents: list[ArtifactIntent] = []
        if isinstance(payload, dict):
            intent = self._try_parse_delivery_intent(payload)
            if intent is not None:
                intents.append(intent)
            for value in payload.values():
                intents.extend(
                    self._extract_explicit_artifact_intents(value, depth + 1)
                )
            return intents

        if isinstance(payload, (list, tuple)):
            for value in payload:
                intents.extend(
                    self._extract_explicit_artifact_intents(value, depth + 1)
                )
            return intents

        if isinstance(payload, str):
            stripped = payload.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    return []
                intents.extend(
                    self._extract_explicit_artifact_intents(parsed, depth + 1)
                )
            return intents

        payload_dict = getattr(payload, "__dict__", None)
        if isinstance(payload_dict, dict):
            intents.extend(
                self._extract_explicit_artifact_intents(payload_dict, depth + 1)
            )
        return intents

    @staticmethod
    def _try_parse_delivery_intent(payload: dict[str, Any]) -> ArtifactIntent | None:
        """Parse one explicit delivery intent dictionary when present."""

        marker = payload.get("delivery_intent", payload.get("deliveryIntent"))
        if marker is not True:
            return None

        path_value = payload.get("artifact_path", payload.get("artifactPath"))
        if not isinstance(path_value, str) or not path_value.strip():
            return None

        caption_value = payload.get("artifact_caption", payload.get("artifactCaption"))
        caption: str | None = None
        if caption_value is not None:
            trimmed = str(caption_value).strip()
            if trimmed:
                caption = trimmed

        return ArtifactIntent(path=path_value.strip(), caption=caption)

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
        return AskResult(
            reply=reply,
            artifact_paths=sorted(self.artifact_paths),
            external_artifact_paths=sorted(self.external_artifact_paths),
            artifact_intents=sorted(
                self.artifact_intents_by_path.values(),
                key=lambda item: item.path,
            ),
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
        binary_download_max_bytes: int = DEFAULT_BINARY_DOWNLOAD_MAX_BYTES,
        binary_download_timeout_seconds: int = DEFAULT_BINARY_DOWNLOAD_TIMEOUT_SECONDS,
        skill_tool_max_calls_per_ask: int = DEFAULT_SKILL_TOOL_MAX_CALLS_PER_ASK,
        require_explicit_artifact_intent: bool = DEFAULT_ARTIFACT_REQUIRE_EXPLICIT_INTENT,
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
        self._skill_tool_max_calls_per_ask = max(1, skill_tool_max_calls_per_ask)
        self._require_explicit_artifact_intent = require_explicit_artifact_intent
        self._tool_call_counts_by_session: dict[str, dict[str, int]] = {}
        self._custom_tools = build_custom_tools(
            working_directory=self._working_directory,
            max_download_bytes=binary_download_max_bytes,
            download_timeout_seconds=binary_download_timeout_seconds,
            max_artifact_bytes=DEFAULT_MAX_ARTIFACT_BYTES,
        )

    @staticmethod
    def _resolve_session_id_for_invocation(invocation: Any) -> str | None:
        """Extract one session identifier from invocation metadata when present."""

        if not isinstance(invocation, dict):
            return None
        session_id = invocation.get("session_id", invocation.get("sessionId"))
        if not isinstance(session_id, str) or not session_id:
            return None
        return session_id

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
                "tools": self._custom_tools,
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
        self, input_data: PreToolUseHookInput, invocation: dict[str, str]
    ) -> PreToolUseHookOutput:
        """Allow tool execution and provide extra context for long-running tasks."""

        tool_name = str(input_data.get("toolName", "unknown"))
        session_id = self._resolve_session_id_for_invocation(invocation)
        if tool_name == "skill" and session_id is not None:
            counts = self._tool_call_counts_by_session.setdefault(session_id, {})
            call_count = counts.get(tool_name, 0) + 1
            counts[tool_name] = call_count
            if call_count > self._skill_tool_max_calls_per_ask:
                logger.warning(
                    "Denying tool call: %s (session_id=%s count=%s cap=%s)",
                    tool_name,
                    session_id,
                    call_count,
                    self._skill_tool_max_calls_per_ask,
                )
                return {
                    "permissionDecision": "deny",
                    "modifiedArgs": input_data.get("toolArgs"),
                    "additionalContext": (
                        "Stop calling the skill tool. Continue with available context "
                        "and provide the best possible answer without additional skill calls."
                    ),
                }
            logger.info(
                "Allowing tool call: %s (session_id=%s count=%s cap=%s)",
                tool_name,
                session_id,
                call_count,
                self._skill_tool_max_calls_per_ask,
            )
        else:
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
            session_id = invocation.get("session_id", invocation.get("sessionId"))
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

        session_id = state.session_id
        if session_id:
            self._tool_call_counts_by_session[session_id] = {}
        collector = AskEventCollector(
            workspace_root=self._working_directory,
            require_explicit_artifact_intent=self._require_explicit_artifact_intent,
        )
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
            if session_id:
                self._tool_call_counts_by_session.pop(session_id, None)

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
                self._tool_call_counts_by_session.pop(session_id, None)

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
            self._tool_call_counts_by_session.clear()

        for state in sessions:
            try:
                await state.session.destroy()
            except Exception:
                logger.exception("Failed to destroy Copilot session")
