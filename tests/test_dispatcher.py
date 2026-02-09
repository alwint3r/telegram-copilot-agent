from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support.fakes import (
    FakeApplication,
    GatedAskManager,
    StaticAskManager,
)
from tests.support.module_loader import import_runtime_module

dispatcher_module = import_runtime_module("copilot_telegram.dispatcher")
models_module = import_runtime_module("copilot_telegram.models")
session_module = import_runtime_module("copilot_telegram.session_manager")


class BackgroundDispatcherShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.BackgroundDispatcher = dispatcher_module.BackgroundDispatcher

    async def test_enqueue_rejected_after_shutdown_requested(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        dispatcher.request_shutdown()

        pending = await dispatcher.enqueue(
            chat_id=99,
            text="hello",
            reply_to_message_id=None,
        )
        self.assertIsNone(pending)

    async def test_shutdown_drains_worker_before_timeout(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())

        async def worker() -> None:
            await asyncio.sleep(0.05)

        worker_task = asyncio.create_task(worker())
        runtime = models_module.ChatRuntime(worker=worker_task)
        async with dispatcher._lock:
            dispatcher._chat_runtimes[1] = runtime

        await asyncio.wait_for(dispatcher.shutdown(), timeout=1)
        self.assertTrue(worker_task.done())
        self.assertFalse(worker_task.cancelled())

    async def test_shutdown_force_mode_cancels_workers_immediately(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())

        block = asyncio.Event()

        async def worker() -> None:
            await block.wait()

        worker_task = asyncio.create_task(worker())
        runtime = models_module.ChatRuntime(worker=worker_task)
        async with dispatcher._lock:
            dispatcher._chat_runtimes[2] = runtime

        dispatcher.request_shutdown(force=True)
        await asyncio.wait_for(dispatcher.shutdown(), timeout=1)
        self.assertTrue(worker_task.cancelled())

    async def test_shutdown_resolves_pending_user_input_futures(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())

        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        pending = models_module.PendingUserInput(
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


class BackgroundDispatcherUserInputReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.BackgroundDispatcher = dispatcher_module.BackgroundDispatcher

    async def _set_pending_input(
        self,
        dispatcher,
        *,
        chat_id: int,
        choices: list[str],
        allow_freeform: bool,
    ) -> asyncio.Future[dict[str, object]]:
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        pending = models_module.PendingUserInput(
            future=future,
            question="Proceed?",
            choices=choices,
            allow_freeform=allow_freeform,
        )
        async with dispatcher._lock:
            dispatcher._pending_user_inputs[chat_id] = pending
        return future

    async def test_consume_user_input_reply_accepts_numeric_choice(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        future = await self._set_pending_input(
            dispatcher,
            chat_id=31,
            choices=["Yes", "No"],
            allow_freeform=False,
        )

        consumed, reply_text = await dispatcher.consume_user_input_reply(
            chat_id=31,
            text="2",
        )
        self.assertTrue(consumed)
        self.assertEqual(reply_text, "Response received. Continuing now.")
        self.assertTrue(future.done())
        self.assertEqual(future.result(), {"answer": "No", "wasFreeform": False})

    async def test_consume_user_input_reply_rejects_invalid_numeric_when_no_freeform(
        self,
    ) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        future = await self._set_pending_input(
            dispatcher,
            chat_id=32,
            choices=["Yes", "No"],
            allow_freeform=False,
        )

        consumed, reply_text = await dispatcher.consume_user_input_reply(
            chat_id=32,
            text="3",
        )
        self.assertTrue(consumed)
        self.assertEqual(reply_text, "Invalid option. Reply with one of: Yes, No")
        self.assertFalse(future.done())

    async def test_consume_user_input_reply_accepts_invalid_numeric_as_freeform(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        future = await self._set_pending_input(
            dispatcher,
            chat_id=33,
            choices=["Yes", "No"],
            allow_freeform=True,
        )

        consumed, reply_text = await dispatcher.consume_user_input_reply(
            chat_id=33,
            text="3",
        )
        self.assertTrue(consumed)
        self.assertEqual(reply_text, "Response received. Continuing now.")
        self.assertTrue(future.done())
        self.assertEqual(future.result(), {"answer": "3", "wasFreeform": True})

    async def test_consume_user_input_reply_trims_numeric_whitespace(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        future = await self._set_pending_input(
            dispatcher,
            chat_id=34,
            choices=["Yes", "No"],
            allow_freeform=False,
        )

        consumed, reply_text = await dispatcher.consume_user_input_reply(
            chat_id=34,
            text=" 1 ",
        )
        self.assertTrue(consumed)
        self.assertEqual(reply_text, "Response received. Continuing now.")
        self.assertTrue(future.done())
        self.assertEqual(future.result(), {"answer": "Yes", "wasFreeform": False})

    async def test_consume_user_input_reply_accepts_case_insensitive_text_choice(
        self,
    ) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        future = await self._set_pending_input(
            dispatcher,
            chat_id=35,
            choices=["Approve", "Deny"],
            allow_freeform=False,
        )

        consumed, reply_text = await dispatcher.consume_user_input_reply(
            chat_id=35,
            text="deny",
        )
        self.assertTrue(consumed)
        self.assertEqual(reply_text, "Response received. Continuing now.")
        self.assertTrue(future.done())
        self.assertEqual(future.result(), {"answer": "Deny", "wasFreeform": False})


class BackgroundDispatcherConcurrencyAndArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.BackgroundDispatcher = dispatcher_module.BackgroundDispatcher

    async def _get_runtime(self, dispatcher, chat_id: int):
        async with dispatcher._lock:
            runtime = dispatcher._chat_runtimes.get(chat_id)
        self.assertIsNotNone(runtime)
        return runtime

    async def test_enqueue_during_worker_stop_transition_does_not_strand_prompt(
        self,
    ) -> None:
        manager = GatedAskManager(models_module.AskResult)
        dispatcher = self.BackgroundDispatcher(FakeApplication(), manager)

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
        manager = StaticAskManager(models_module.AskResult)
        dispatcher = self.BackgroundDispatcher(FakeApplication(), manager)
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
        manager = StaticAskManager(
            models_module.AskResult,
            ask_result=models_module.AskResult(reply="done", artifact_paths=[]),
        )
        dispatcher = self.BackgroundDispatcher(FakeApplication(), manager)
        bot = dispatcher._application.bot

        try:
            await dispatcher.enqueue(chat_id=21, text="hello", reply_to_message_id=None)
            runtime = await self._get_runtime(dispatcher, 21)
            await asyncio.wait_for(runtime.queue.join(), timeout=1)

            self.assertEqual(manager.prompts, ["hello"])
            self.assertEqual(bot.sent_documents, [])
            self.assertEqual(bot.sent_photos, [])
        finally:
            await dispatcher.shutdown()

    async def test_send_artifacts_skips_resolved_paths_outside_workspace(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        bot = dispatcher._application.bot

        with tempfile.TemporaryDirectory() as workspace:
            with tempfile.TemporaryDirectory() as outside:
                outside_file = Path(outside, "secret.zip")
                outside_file.write_bytes(b"secret")
                symlink_path = Path(workspace, "leak.zip")
                symlink_path.symlink_to(outside_file)

                with patch.object(dispatcher_module.os, "getcwd", return_value=workspace):
                    await dispatcher._send_artifacts(
                        bot=bot,
                        chat_id=1,
                        artifact_candidates=[str(symlink_path)],
                        reply_to_message_id=None,
                    )

        self.assertEqual(bot.sent_documents, [])
        self.assertEqual(bot.sent_photos, [])

    async def test_send_artifacts_allows_in_workspace_file(self) -> None:
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        bot = dispatcher._application.bot

        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "artifact.zip")
            artifact.write_bytes(b"artifact")

            with patch.object(dispatcher_module.os, "getcwd", return_value=workspace):
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
        dispatcher = self.BackgroundDispatcher(FakeApplication(), object())
        bot = dispatcher._application.bot

        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "artifact.zip")

            collector = session_module.AskEventCollector(workspace_root=workspace)
            collector.collect_path(str(artifact))
            ask_result = collector.build_result()
            self.assertEqual(ask_result.artifact_paths, [str(artifact.resolve())])

            artifact.write_bytes(b"artifact")
            with patch.object(dispatcher_module.os, "getcwd", return_value=workspace):
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


if __name__ == "__main__":
    unittest.main()
