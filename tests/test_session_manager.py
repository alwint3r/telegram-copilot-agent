from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from tests.support.fakes import FakeClient
from tests.support.module_loader import import_runtime_module

config_module = import_runtime_module("copilot_telegram.config")
session_module = import_runtime_module("copilot_telegram.session_manager")


class CopilotSessionManagerResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.CopilotSessionManager = session_module.CopilotSessionManager

    async def test_reset_missing_session_returns_false(self) -> None:
        client = FakeClient()
        manager = self.CopilotSessionManager(
            client=client, model="gpt-5", timeout_seconds=5
        )
        self.assertFalse(await manager.reset_session(chat_id=42))
        await manager.shutdown()

    async def test_reset_session_creates_fresh_history_on_next_ask(self) -> None:
        client = FakeClient()
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
        client = FakeClient()
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
        client = FakeClient()
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
        client = FakeClient()
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
        client = FakeClient()
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
        client = FakeClient()
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
        client = FakeClient()
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
            config = config_module.load_startup_config()
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
            config = config_module.load_startup_config()
        self.assertIsNone(config.reasoning_effort)

    def test_model_supports_reasoning_effort_for_gpt5_plus(self) -> None:
        self.assertTrue(config_module.model_supports_reasoning_effort("gpt-5"))
        self.assertTrue(config_module.model_supports_reasoning_effort("gpt-6-mini"))
        self.assertFalse(config_module.model_supports_reasoning_effort("gpt-4.1"))
        self.assertFalse(
            config_module.model_supports_reasoning_effort("claude-sonnet-4.5")
        )


if __name__ == "__main__":
    unittest.main()
