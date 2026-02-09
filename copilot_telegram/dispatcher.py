"""Background prompt queueing and per-chat worker execution."""

import asyncio
from contextlib import suppress
import logging
import mimetypes
import os
from pathlib import Path
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from copilot.types import UserInputRequest, UserInputResponse
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import Application

from .artifacts import is_sendable_artifact
from .config import load_dispatcher_config
from .constants import (
    HEARTBEAT_INTERVAL_SECONDS,
    PROGRESS_EDIT_THROTTLE_SECONDS,
    PROGRESS_PREVIEW_CHARS,
)
from .models import AskResult, ChatRuntime, PendingUserInput, PromptItem
from .session_manager import CopilotSessionManager
from .text_utils import split_for_telegram
from .user_input import default_user_input_answer

logger = logging.getLogger(__name__)


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
