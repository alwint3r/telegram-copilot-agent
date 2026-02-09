from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_chat_telegram_module() -> types.ModuleType:
    module_name = "chat_telegram_under_test"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    copilot_module = types.ModuleType("copilot")
    copilot_module.CopilotClient = object
    sys.modules["copilot"] = copilot_module

    copilot_types_module = types.ModuleType("copilot.types")
    for alias in (
        "CopilotClientOptions",
        "LogLevel",
        "PreToolUseHookInput",
        "PreToolUseHookOutput",
        "SessionConfig",
        "UserInputRequest",
        "UserInputResponse",
    ):
        setattr(copilot_types_module, alias, dict)
    sys.modules["copilot.types"] = copilot_types_module

    dotenv_module = types.ModuleType("dotenv")
    dotenv_module.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv_module

    telegram_module = types.ModuleType("telegram")
    telegram_module.Update = object
    sys.modules["telegram"] = telegram_module

    telegram_constants = types.ModuleType("telegram.constants")

    class ChatAction:
        TYPING = "typing"

    telegram_constants.ChatAction = ChatAction
    sys.modules["telegram.constants"] = telegram_constants

    telegram_error = types.ModuleType("telegram.error")

    class BadRequest(Exception):
        pass

    telegram_error.BadRequest = BadRequest
    sys.modules["telegram.error"] = telegram_error

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.Application = object
    telegram_ext.CommandHandler = object
    telegram_ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    telegram_ext.MessageHandler = object
    telegram_ext.filters = types.SimpleNamespace(TEXT=1, COMMAND=2)
    sys.modules["telegram.ext"] = telegram_ext

    module_path = Path(__file__).with_name("chat-telegram.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load chat-telegram.py for tests")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeSession:
    def __init__(
        self,
        session_id: str,
        send_gate: asyncio.Event | None = None,
        terminal_error: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.send_gate = send_gate
        self.terminal_error = terminal_error
        self.send_calls = 0
        self.destroy_calls = 0
        self._handlers: list[types.FunctionType] = []

    def on(self, handler):
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    def _emit(self, event_type: str, data: object | None) -> None:
        event = types.SimpleNamespace(
            type=types.SimpleNamespace(value=event_type), data=data
        )
        for handler in list(self._handlers):
            handler(event)

    async def send(self, _payload: dict[str, str]) -> None:
        self.send_calls += 1
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.terminal_error is not None:
            self._emit(
                "session.error",
                types.SimpleNamespace(
                    message=self.terminal_error,
                    progress_message=self.terminal_error,
                ),
            )
            return
        self._emit(
            "assistant.message",
            types.SimpleNamespace(content=f"reply:{self.session_id}"),
        )
        self._emit("session.idle", None)

    async def destroy(self) -> None:
        self.destroy_calls += 1


class _FakeClient:
    def __init__(self) -> None:
        self.created_sessions: list[_FakeSession] = []
        self.create_session_configs: list[dict[str, object]] = []
        self._counter = 0
        self.first_session_send_gate: asyncio.Event | None = None
        self.first_session_terminal_error: str | None = None
        self.models_rejecting_reasoning_effort: set[str] = set()

    async def create_session(self, config: dict[str, object]) -> _FakeSession:
        model = str(config.get("model") or "")
        self.create_session_configs.append(dict(config))
        if (
            "reasoning_effort" in config
            and model in self.models_rejecting_reasoning_effort
        ):
            raise RuntimeError(
                f"Model '{model}' does not support reasoning effort configuration."
            )

        self._counter += 1
        session_id = f"s{self._counter}"
        gate = self.first_session_send_gate if self._counter == 1 else None
        terminal_error = (
            self.first_session_terminal_error if self._counter == 1 else None
        )
        session = _FakeSession(
            session_id=session_id,
            send_gate=gate,
            terminal_error=terminal_error,
        )
        self.created_sessions.append(session)
        return session


class _FakeBot:
    def __init__(self) -> None:
        self._message_id = 0
        self.sent_messages: list[dict[str, object]] = []
        self.sent_photos: list[dict[str, object]] = []
        self.sent_documents: list[dict[str, object]] = []
        self.fail_send_message_count = 0

    async def send_message(self, chat_id: int, text: str, **_kwargs):
        if self.fail_send_message_count > 0:
            self.fail_send_message_count -= 1
            raise RuntimeError("send_message failed")
        self._message_id += 1
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        payload.update(_kwargs)
        self.sent_messages.append(payload)
        return types.SimpleNamespace(message_id=self._message_id, text=text)

    async def edit_message_text(self, **_kwargs) -> None:
        return None

    async def send_chat_action(self, **_kwargs) -> None:
        return None

    async def send_photo(self, **_kwargs) -> None:
        self.sent_photos.append(dict(_kwargs))
        return None

    async def send_document(self, **_kwargs) -> None:
        self.sent_documents.append(dict(_kwargs))
        return None


class _FakeApplication:
    def __init__(self) -> None:
        self.bot = _FakeBot()
        self.bot_data: dict[str, object] = {}


class _GatedAskManager:
    def __init__(self, module: types.ModuleType) -> None:
        self._module = module
        self.prompts: list[str] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.second_seen = asyncio.Event()

    async def ask(self, chat_id: int, prompt: str, progress_callback=None):
        _ = chat_id, progress_callback
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            self.first_started.set()
            await self.release_first.wait()
        if len(self.prompts) >= 2:
            self.second_seen.set()
        return self._module.AskResult(reply=f"reply:{prompt}", artifact_paths=[])


class _StaticAskManager:
    def __init__(self, module: types.ModuleType, ask_result=None) -> None:
        self._module = module
        self.prompts: list[str] = []
        self.ask_result = ask_result

    async def ask(self, chat_id: int, prompt: str, progress_callback=None):
        _ = chat_id, progress_callback
        self.prompts.append(prompt)
        if self.ask_result is not None:
            return self.ask_result
        return self._module.AskResult(reply=f"reply:{prompt}", artifact_paths=[])


class CopilotSessionManagerResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        module = _load_chat_telegram_module()
        self.CopilotSessionManager = module.CopilotSessionManager

    async def test_reset_missing_session_returns_false(self) -> None:
        client = _FakeClient()
        manager = self.CopilotSessionManager(
            client=client, model="gpt-5", timeout_seconds=5
        )
        self.assertFalse(await manager.reset_session(chat_id=42))
        await manager.shutdown()

    async def test_reset_session_creates_fresh_history_on_next_ask(self) -> None:
        client = _FakeClient()
        manager = self.CopilotSessionManager(
            client=client, model="gpt-5", timeout_seconds=5
        )

        first = await manager.ask(chat_id=7, prompt="hello")
        self.assertEqual(first.reply, "reply:s1")

        self.assertTrue(await manager.reset_session(chat_id=7))
        self.assertEqual(client.created_sessions[0].destroy_calls, 1)

        second = await manager.ask(chat_id=7, prompt="hello again")
        self.assertEqual(second.reply, "reply:s2")
        await manager.shutdown()

    async def test_reset_drains_inflight_request_before_destroy(self) -> None:
        client = _FakeClient()
        client.first_session_send_gate = asyncio.Event()
        manager = self.CopilotSessionManager(
            client=client, model="gpt-5", timeout_seconds=5
        )

        ask_task = asyncio.create_task(manager.ask(chat_id=9, prompt="slow request"))

        while not client.created_sessions or client.created_sessions[0].send_calls == 0:
            await asyncio.sleep(0.01)

        reset_task = asyncio.create_task(manager.reset_session(chat_id=9))
        await asyncio.sleep(0.05)
        self.assertFalse(reset_task.done())

        client.first_session_send_gate.set()

        ask_result = await ask_task
        self.assertEqual(ask_result.reply, "reply:s1")

        self.assertTrue(await reset_task)
        self.assertEqual(client.created_sessions[0].destroy_calls, 1)

        next_result = await manager.ask(chat_id=9, prompt="fresh request")
        self.assertEqual(next_result.reply, "reply:s2")
        await manager.shutdown()

    async def test_ask_retries_when_state_was_closed_before_lock(self) -> None:
        client = _FakeClient()
        manager = self.CopilotSessionManager(
            client=client, model="gpt-5", timeout_seconds=5
        )

        initial_state = await manager._get_or_create_session(chat_id=15)
        await initial_state.lock.acquire()

        ask_task = asyncio.create_task(manager.ask(chat_id=15, prompt="retry needed"))
        await asyncio.sleep(0.05)

        reset_task = asyncio.create_task(manager.reset_session(chat_id=15))
        await asyncio.sleep(0.05)

        initial_state.lock.release()

        self.assertTrue(await reset_task)
        result = await ask_task
        self.assertEqual(result.reply, "reply:s2")
        self.assertEqual(client.created_sessions[0].send_calls, 0)

        await manager.shutdown()

    async def test_session_config_includes_reasoning_effort_for_gpt5(self) -> None:
        client = _FakeClient()
        manager = self.CopilotSessionManager(
            client=client,
            model="gpt-5",
            timeout_seconds=5,
            reasoning_effort="medium",
        )

        await manager.ask(chat_id=3, prompt="test")
        self.assertEqual(
            client.create_session_configs[0].get("reasoning_effort"), "medium"
        )
        await manager.shutdown()

    async def test_session_config_omits_reasoning_effort_for_gpt4(self) -> None:
        client = _FakeClient()
        manager = self.CopilotSessionManager(
            client=client,
            model="gpt-4.1",
            timeout_seconds=5,
            reasoning_effort="medium",
        )

        await manager.ask(chat_id=4, prompt="test")
        self.assertNotIn("reasoning_effort", client.create_session_configs[0])
        await manager.shutdown()

    async def test_session_creation_retries_without_reasoning_effort(self) -> None:
        client = _FakeClient()
        client.models_rejecting_reasoning_effort.add("gpt-5.1-mini")
        manager = self.CopilotSessionManager(
            client=client,
            model="gpt-5.1-mini",
            timeout_seconds=5,
            reasoning_effort="medium",
        )

        result = await manager.ask(chat_id=10, prompt="test")
        self.assertEqual(result.reply, "reply:s1")
        self.assertEqual(len(client.create_session_configs), 2)
        self.assertEqual(
            client.create_session_configs[0].get("reasoning_effort"), "medium"
        )
        self.assertNotIn("reasoning_effort", client.create_session_configs[1])
        await manager.shutdown()

    async def test_ask_raises_on_terminal_session_error_without_idle(self) -> None:
        client = _FakeClient()
        client.first_session_terminal_error = "backend unavailable"
        manager = self.CopilotSessionManager(
            client=client, model="gpt-5", timeout_seconds=30
        )

        with self.assertRaisesRegex(
            RuntimeError, r"session\.error: backend unavailable"
        ):
            await asyncio.wait_for(
                manager.ask(chat_id=11, prompt="hello"),
                timeout=1,
            )
        await manager.shutdown()


class StartupConfigReasoningEffortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_chat_telegram_module()

    def test_load_startup_config_parses_reasoning_effort(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_API_KEY": "token",
                "COPILOT_MODEL": "gpt-5",
                "COPILOT_REASONING_EFFORT": "HIGH",
            },
            clear=True,
        ):
            config = self.module.load_startup_config()
        self.assertEqual(config.reasoning_effort, "high")

    def test_load_startup_config_ignores_invalid_reasoning_effort(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_API_KEY": "token",
                "COPILOT_REASONING_EFFORT": "ultra",
            },
            clear=True,
        ):
            config = self.module.load_startup_config()
        self.assertIsNone(config.reasoning_effort)

    def test_model_supports_reasoning_effort_for_gpt5_plus(self) -> None:
        self.assertTrue(self.module.model_supports_reasoning_effort("gpt-5"))
        self.assertTrue(self.module.model_supports_reasoning_effort("gpt-6-mini"))
        self.assertFalse(self.module.model_supports_reasoning_effort("gpt-4.1"))
        self.assertFalse(
            self.module.model_supports_reasoning_effort("claude-sonnet-4.5")
        )


class BackgroundDispatcherShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_chat_telegram_module()
        self.BackgroundDispatcher = self.module.BackgroundDispatcher

    async def test_enqueue_rejected_after_shutdown_requested(self) -> None:
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), object())
        dispatcher.request_shutdown()

        pending = await dispatcher.enqueue(
            chat_id=99,
            text="hello",
            reply_to_message_id=None,
        )
        self.assertIsNone(pending)

    async def test_shutdown_drains_worker_before_timeout(self) -> None:
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), object())

        async def worker() -> None:
            await asyncio.sleep(0.05)

        worker_task = asyncio.create_task(worker())
        runtime = self.module.ChatRuntime(worker=worker_task)
        async with dispatcher._lock:
            dispatcher._chat_runtimes[1] = runtime

        await asyncio.wait_for(dispatcher.shutdown(), timeout=1)
        self.assertTrue(worker_task.done())
        self.assertFalse(worker_task.cancelled())

    async def test_shutdown_force_mode_cancels_workers_immediately(self) -> None:
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), object())

        block = asyncio.Event()

        async def worker() -> None:
            await block.wait()

        worker_task = asyncio.create_task(worker())
        runtime = self.module.ChatRuntime(worker=worker_task)
        async with dispatcher._lock:
            dispatcher._chat_runtimes[2] = runtime

        dispatcher.request_shutdown(force=True)
        await asyncio.wait_for(dispatcher.shutdown(), timeout=1)
        self.assertTrue(worker_task.cancelled())

    async def test_shutdown_resolves_pending_user_input_futures(self) -> None:
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), object())

        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        pending = self.module.PendingUserInput(
            future=future,
            question="Proceed?",
            choices=["Yes", "No"],
            allow_freeform=False,
        )
        async with dispatcher._lock:
            dispatcher._pending_user_inputs[5] = pending

        await dispatcher.shutdown()
        self.assertTrue(future.done())
        self.assertEqual(future.result(), {"answer": "No", "wasFreeform": False})


class BackgroundDispatcherConcurrencyAndArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_chat_telegram_module()
        self.BackgroundDispatcher = self.module.BackgroundDispatcher

    async def _get_runtime(self, dispatcher, chat_id: int):
        async with dispatcher._lock:
            runtime = dispatcher._chat_runtimes.get(chat_id)
        self.assertIsNotNone(runtime)
        return runtime

    async def test_enqueue_during_worker_stop_transition_does_not_strand_prompt(
        self,
    ) -> None:
        manager = _GatedAskManager(self.module)
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), manager)

        stop_transition_reached = asyncio.Event()
        original_mark = dispatcher._mark_runtime_worker_stopped

        def wrapped_mark(chat_id: int, runtime) -> None:
            stop_transition_reached.set()
            original_mark(chat_id, runtime)

        dispatcher._mark_runtime_worker_stopped = wrapped_mark

        try:
            await dispatcher.enqueue(chat_id=17, text="first", reply_to_message_id=None)
            await asyncio.wait_for(manager.first_started.wait(), timeout=1)

            await dispatcher._lock.acquire()
            try:
                enqueue_second_task = asyncio.create_task(
                    dispatcher.enqueue(
                        chat_id=17,
                        text="second",
                        reply_to_message_id=None,
                    )
                )
                manager.release_first.set()
                try:
                    await asyncio.wait_for(stop_transition_reached.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
            finally:
                dispatcher._lock.release()

            pending = await asyncio.wait_for(enqueue_second_task, timeout=1)
            self.assertIsNotNone(pending)

            await asyncio.wait_for(manager.second_seen.wait(), timeout=1)
            runtime = await self._get_runtime(dispatcher, 17)
            await asyncio.wait_for(runtime.queue.join(), timeout=1)
        finally:
            await dispatcher.shutdown()

    async def test_worker_cleans_queue_when_reporter_creation_send_fails(self) -> None:
        manager = _StaticAskManager(self.module)
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), manager)
        bot = dispatcher._application.bot
        bot.fail_send_message_count = 1

        try:
            await dispatcher.enqueue(chat_id=18, text="first", reply_to_message_id=None)
            await dispatcher.enqueue(chat_id=18, text="second", reply_to_message_id=None)

            runtime = await self._get_runtime(dispatcher, 18)
            await asyncio.wait_for(runtime.queue.join(), timeout=1)

            self.assertEqual(manager.prompts, ["second"])
            self.assertTrue(
                any(
                    message["text"] == "I hit an error while contacting Copilot."
                    for message in bot.sent_messages
                )
            )
            self.assertTrue(
                any(message["text"] == "reply:second" for message in bot.sent_messages)
            )
        finally:
            await dispatcher.shutdown()

    async def test_worker_uses_only_explicit_artifact_paths(self) -> None:
        manager = _StaticAskManager(
            self.module,
            ask_result=self.module.AskResult(reply="done", artifact_paths=[]),
        )
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), manager)
        diff_scan_called = False

        def mark_diff_scan(*_args, **_kwargs):
            nonlocal diff_scan_called
            diff_scan_called = True
            return []

        try:
            with patch.object(
                self.module,
                "find_new_or_modified_files",
                side_effect=mark_diff_scan,
            ):
                await dispatcher.enqueue(chat_id=21, text="hello", reply_to_message_id=None)
                runtime = await self._get_runtime(dispatcher, 21)
                await asyncio.wait_for(runtime.queue.join(), timeout=1)

            self.assertEqual(manager.prompts, ["hello"])
            self.assertFalse(diff_scan_called)
        finally:
            await dispatcher.shutdown()

    async def test_send_artifacts_skips_resolved_paths_outside_workspace(self) -> None:
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), object())
        bot = dispatcher._application.bot

        with tempfile.TemporaryDirectory() as workspace:
            with tempfile.TemporaryDirectory() as outside:
                outside_file = Path(outside, "secret.zip")
                outside_file.write_bytes(b"secret")
                symlink_path = Path(workspace, "leak.zip")
                symlink_path.symlink_to(outside_file)

                with patch.object(self.module.os, "getcwd", return_value=workspace):
                    await dispatcher._send_artifacts(
                        bot=bot,
                        chat_id=1,
                        artifact_candidates=[str(symlink_path)],
                        reply_to_message_id=None,
                    )

        self.assertEqual(bot.sent_documents, [])
        self.assertEqual(bot.sent_photos, [])

    async def test_send_artifacts_allows_in_workspace_file(self) -> None:
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), object())
        bot = dispatcher._application.bot

        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "artifact.zip")
            artifact.write_bytes(b"artifact")

            with patch.object(self.module.os, "getcwd", return_value=workspace):
                await dispatcher._send_artifacts(
                    bot=bot,
                    chat_id=1,
                    artifact_candidates=[str(artifact)],
                    reply_to_message_id=None,
                )

        self.assertEqual(len(bot.sent_documents), 1)
        self.assertEqual(len(bot.sent_photos), 0)
        self.assertTrue(
            any("Sent 1 artifact(s)." == message["text"] for message in bot.sent_messages)
        )

    async def test_send_artifacts_supports_paths_collected_before_file_exists(self) -> None:
        dispatcher = self.BackgroundDispatcher(_FakeApplication(), object())
        bot = dispatcher._application.bot

        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "artifact.zip")

            collector = self.module.AskEventCollector(workspace_root=workspace)
            collector.collect_path(str(artifact))
            ask_result = collector.build_result()
            self.assertEqual(ask_result.artifact_paths, [str(artifact.resolve())])

            artifact.write_bytes(b"artifact")
            with patch.object(self.module.os, "getcwd", return_value=workspace):
                await dispatcher._send_artifacts(
                    bot=bot,
                    chat_id=1,
                    artifact_candidates=list(ask_result.artifact_paths),
                    reply_to_message_id=None,
                )

        self.assertEqual(len(bot.sent_documents), 1)
        self.assertEqual(len(bot.sent_photos), 0)
        self.assertTrue(
            any("Sent 1 artifact(s)." == message["text"] for message in bot.sent_messages)
        )


class ChatTelegramUtilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_chat_telegram_module()

    def test_split_for_telegram_prefers_newline_boundaries(self) -> None:
        text = "alpha line\nbeta line\ngamma line"
        chunks = self.module.split_for_telegram(text, chunk_size=12)
        self.assertEqual(chunks, ["alpha line\n", "beta line\n", "gamma line"])

    def test_split_for_telegram_preserves_whitespace_round_trip(self) -> None:
        text = "0123456789\n    indented line\n\nnext line"
        chunks = self.module.split_for_telegram(text, chunk_size=14)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 14 for chunk in chunks))
        self.assertTrue(any(chunk.startswith("    ") for chunk in chunks))

    def test_build_result_keeps_artifacts_when_reply_missing(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "artifact.bin")
            artifact.write_bytes(b"artifact")

            collector = self.module.AskEventCollector(workspace_root=workspace)
            collector.collect_path(str(artifact))
            result = collector.build_result()

            self.assertEqual(result.reply, "I could not generate a response.")
            self.assertEqual(result.artifact_paths, [str(artifact.resolve())])

    def test_collect_path_keeps_in_workspace_candidate_before_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "later.bin")
            self.assertFalse(artifact.exists())

            collector = self.module.AskEventCollector(workspace_root=workspace)
            collector.collect_path(str(artifact))
            result = collector.build_result()

            self.assertEqual(result.artifact_paths, [str(artifact.resolve())])

    def test_normalize_workspace_path_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            inside = Path(workspace, "inside.txt")
            inside.write_text("ok", encoding="utf-8")

            normalized_inside = self.module.normalize_workspace_path(
                "inside.txt", workspace
            )
            escaped = self.module.normalize_workspace_path("../outside.txt", workspace)

            self.assertEqual(normalized_inside, str(inside.resolve()))
            self.assertIsNone(escaped)

    def test_extract_existing_paths_from_obj_finds_nested_file(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "artifact.bin")
            artifact.write_bytes(b"artifact")

            payload = {
                "level1": [
                    "not-a-path",
                    {
                        "deep": {
                            "path": str(artifact),
                        }
                    },
                ]
            }

            found = self.module.extract_existing_paths_from_obj(payload, workspace)
            self.assertEqual(found, {str(artifact.resolve())})

    def test_is_sendable_artifact_filters_text_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            image_path = Path(workspace, "plot.png")
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            text_path = Path(workspace, "notes.txt")
            text_path.write_text("hello", encoding="utf-8")

            self.assertTrue(
                self.module.is_sendable_artifact(
                    str(image_path), max_artifact_bytes=1024
                )
            )
            self.assertFalse(
                self.module.is_sendable_artifact(
                    str(text_path), max_artifact_bytes=1024
                )
            )
            self.assertFalse(
                self.module.is_sendable_artifact(str(image_path), max_artifact_bytes=1)
            )

    def test_default_user_input_answer_prefers_negative_choice(self) -> None:
        answer = self.module.default_user_input_answer(
            choices=["Yes", "No", "Skip"], allow_freeform=False
        )
        self.assertEqual(answer, {"answer": "No", "wasFreeform": False})


if __name__ == "__main__":
    unittest.main()
