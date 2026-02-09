"""Background prompt queueing and per-chat worker execution."""

import asyncio
from contextlib import suppress
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from copilot.types import UserInputRequest, UserInputResponse
from telegram.constants import ChatAction
from telegram.error import BadRequest, TimedOut
from telegram.ext import Application

from .artifacts import is_sendable_artifact
from .config import load_dispatcher_config
from .constants import (
    HEARTBEAT_INTERVAL_SECONDS,
    PROGRESS_EDIT_THROTTLE_SECONDS,
    PROGRESS_PREVIEW_CHARS,
)
from .models import (
    ArtifactDeliveryReport,
    ArtifactIntent,
    AskResult,
    ChatRuntime,
    PendingUserInput,
    PromptItem,
)
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
        artifact_candidates: list[ArtifactIntent],
        reply_to_message_id: int | None,
        allowed_roots: list[str] | None = None,
    ) -> ArtifactDeliveryReport:
        """Send eligible generated artifacts back to Telegram chat."""

        report = ArtifactDeliveryReport()
        seen: set[str] = set()
        roots = allowed_roots or [os.getcwd()]
        allowed_root_paths = [
            Path(os.path.realpath(root)).resolve(strict=False) for root in roots
        ]
        for artifact in artifact_candidates:
            normalized = os.path.realpath(artifact.path)
            normalized_path = Path(normalized)
            file_label = os.path.basename(normalized) or normalized

            if not any(
                self._is_path_within_root(normalized_path, root)
                for root in allowed_root_paths
            ):
                logger.warning("Skipping artifact outside allowed roots: %s", normalized)
                report.skipped.append(f"{file_label}: outside allowed roots")
                continue

            if normalized in seen:
                report.skipped.append(f"{file_label}: duplicate candidate")
                continue
            seen.add(normalized)

            if not is_sendable_artifact(normalized, self._config.max_artifact_bytes):
                report.skipped.append(f"{file_label}: not sendable (type/size/path)")
                continue
            if report.sent >= self._config.max_artifacts_per_request:
                report.skipped.append(
                    f"{file_label}: over per-request artifact limit "
                    f"({self._config.max_artifacts_per_request})"
                )
                break

            extension = Path(normalized).suffix.lower()
            mime_type, _ = mimetypes.guess_type(normalized)
            caption = artifact.caption or f"Artifact: {file_label}"
            report.attempted += 1

            try:
                max_attempts = 1 + self._config.artifact_send_retries
                last_timeout: Exception | None = None
                sent = False
                for attempt in range(max_attempts):
                    timeout_seconds = self._config.artifact_send_timeout_seconds * (
                        attempt + 1
                    )
                    timeout_kwargs = {
                        "connect_timeout": timeout_seconds,
                        "write_timeout": timeout_seconds,
                        "read_timeout": timeout_seconds,
                        "pool_timeout": timeout_seconds,
                    }
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
                                    **timeout_kwargs,
                                )
                        else:
                            with open(normalized, "rb") as file_obj:
                                await bot.send_document(
                                    chat_id=chat_id,
                                    document=file_obj,
                                    caption=caption,
                                    reply_to_message_id=reply_to_message_id,
                                    allow_sending_without_reply=True,
                                    **timeout_kwargs,
                                )
                        sent = True
                        break
                    except (TimedOut, TimeoutError) as exc:
                        last_timeout = exc
                        if attempt + 1 >= max_attempts:
                            raise
                        logger.warning(
                            "Timed out sending artifact %s (attempt %s/%s). Retrying.",
                            normalized,
                            attempt + 1,
                            max_attempts,
                        )
                if not sent and last_timeout is not None:
                    raise last_timeout
                report.sent += 1
            except Exception as exc:
                report.failed.append(f"{file_label}: {exc}")
                logger.exception("Failed to send artifact %s", normalized)

        if report.sent > 0:
            await bot.send_message(
                chat_id=chat_id,
                text=f"Sent {report.sent} artifact(s).",
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        return report

    @staticmethod
    def _is_path_within_root(path: Path, root: Path) -> bool:
        """Return True when a path is inside a root directory."""

        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _is_allowed_external_source(self, source_path: Path) -> bool:
        """Return True when an external artifact source path is allowed."""

        if not self._config.artifact_allow_tmp_sources_only:
            return True
        tmp_root = Path(os.path.realpath(tempfile.gettempdir())).resolve(strict=False)
        return self._is_path_within_root(source_path, tmp_root)

    def _stage_external_artifacts(
        self,
        chat_id: int,
        request_id: int,
        external_candidates: list[ArtifactIntent],
    ) -> tuple[list[ArtifactIntent], Path | None]:
        """Copy allowed external artifacts into managed temp storage for sending."""

        if not external_candidates:
            return [], None

        staging_root = Path(self._config.artifact_temp_root).resolve(strict=False)
        request_dir = (
            staging_root
            / f"chat-{chat_id}"
            / f"request-{request_id}-{uuid.uuid4().hex[:8]}"
        )

        try:
            request_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Failed to create staging directory: %s", request_dir)
            return [], None

        staged_paths: list[ArtifactIntent] = []
        seen_sources: set[str] = set()

        for candidate in external_candidates:
            source = Path(os.path.realpath(candidate.path))
            source_str = str(source)
            if source_str in seen_sources:
                continue
            seen_sources.add(source_str)

            if not source.is_file():
                continue
            if not self._is_allowed_external_source(source):
                logger.warning("Skipping non-temp external artifact source: %s", source)
                continue
            if not is_sendable_artifact(source_str, self._config.max_artifact_bytes):
                continue

            destination = request_dir / source.name
            if destination.exists():
                destination = request_dir / f"{source.stem}-{uuid.uuid4().hex[:8]}{source.suffix}"

            try:
                shutil.copy2(source, destination)
            except OSError:
                logger.exception("Failed to stage artifact %s", source)
                continue

            staged_paths.append(
                ArtifactIntent(
                    path=str(destination.resolve(strict=False)),
                    caption=candidate.caption,
                )
            )
            if len(staged_paths) >= self._config.max_artifacts_per_request:
                break

        return staged_paths, request_dir

    def _cleanup_staged_artifact_dir(self, request_dir: Path | None) -> None:
        """Best-effort cleanup for one request staging directory."""

        if request_dir is None:
            return
        staging_root = Path(self._config.artifact_temp_root).resolve(strict=False)

        try:
            shutil.rmtree(request_dir)
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Failed to cleanup staged artifacts: %s", request_dir)
            return

        parent = request_dir.parent
        while parent != staging_root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

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

    def _collect_artifact_candidates(
        self, ask_result: AskResult
    ) -> tuple[list[ArtifactIntent], list[ArtifactIntent]]:
        """Collect direct and external candidates with explicit intent priority."""

        workspace_root = Path(os.path.realpath(os.getcwd())).resolve(strict=False)
        direct: list[ArtifactIntent] = []
        external: list[ArtifactIntent] = []
        direct_by_path: dict[str, ArtifactIntent] = {}
        external_by_path: dict[str, ArtifactIntent] = {}

        def add_direct_candidate(path_value: str, caption: str | None) -> None:
            normalized = os.path.realpath(path_value)
            existing = direct_by_path.get(normalized)
            if existing is None:
                direct_by_path[normalized] = ArtifactIntent(path=normalized, caption=caption)
                return
            if existing.caption is None and caption:
                direct_by_path[normalized] = ArtifactIntent(path=normalized, caption=caption)

        def add_external_candidate(path_value: str, caption: str | None) -> None:
            normalized = os.path.realpath(path_value)
            existing = external_by_path.get(normalized)
            if existing is None:
                external_by_path[normalized] = ArtifactIntent(
                    path=normalized, caption=caption
                )
                return
            if existing.caption is None and caption:
                external_by_path[normalized] = ArtifactIntent(
                    path=normalized, caption=caption
                )

        for intent in ask_result.artifact_intents:
            if self._is_path_within_root(Path(os.path.realpath(intent.path)), workspace_root):
                add_direct_candidate(intent.path, intent.caption)
            else:
                add_external_candidate(intent.path, intent.caption)
        if not self._config.artifact_require_explicit_intent:
            for artifact_path in ask_result.artifact_paths:
                add_direct_candidate(artifact_path, None)
            for external_path in ask_result.external_artifact_paths:
                add_external_candidate(external_path, None)

        direct.extend(direct_by_path.values())
        external.extend(external_by_path.values())
        return direct, external

    async def _send_artifact_delivery_summary(
        self,
        bot: Any,
        chat_id: int,
        reply_to_message_id: int | None,
        report: ArtifactDeliveryReport,
    ) -> None:
        """Send user-visible artifact delivery outcome when failures occur."""

        reasons = [*report.failed, *report.skipped]
        if not reasons:
            return

        detail = "; ".join(reasons[:2])
        if report.sent == 0:
            total = report.attempted + len(report.skipped)
            text = (
                f"I found {total} artifact(s) but could not deliver them. "
                f"{detail}"
            )
        elif report.failed:
            text = (
                f"Delivered {report.sent} artifact(s), but {len(report.failed)} failed. "
                f"{detail}"
            )
        else:
            return

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        except Exception:
            logger.exception(
                "Failed to send artifact delivery summary for chat_id=%s", chat_id
            )

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
            staged_dir: Path | None = None
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
                    f"Request {item.request_id}: done. Sending response."
                )
                await self._send_reply_chunks(
                    bot=bot,
                    chat_id=chat_id,
                    reply_to_message_id=item.reply_to_message_id,
                    reply=ask_result.reply,
                )

                direct_candidates, external_candidates = self._collect_artifact_candidates(
                    ask_result=ask_result,
                )
                staged_candidates, staged_dir = self._stage_external_artifacts(
                    chat_id=chat_id,
                    request_id=item.request_id,
                    external_candidates=external_candidates,
                )
                artifact_candidates = [*direct_candidates, *staged_candidates]
                delivery_report = await self._send_artifacts(
                    bot=bot,
                    chat_id=chat_id,
                    artifact_candidates=artifact_candidates,
                    reply_to_message_id=item.reply_to_message_id,
                    allowed_roots=[os.getcwd(), self._config.artifact_temp_root],
                )
                await self._send_artifact_delivery_summary(
                    bot=bot,
                    chat_id=chat_id,
                    reply_to_message_id=item.reply_to_message_id,
                    report=delivery_report,
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
                self._cleanup_staged_artifact_dir(staged_dir)
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
