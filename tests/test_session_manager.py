from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from tests.support.fakes import FakeClient
from tests.support.module_loader import import_runtime_module

config_module = import_runtime_module("copilot_telegram.config")
constants_module = import_runtime_module("copilot_telegram.constants")
custom_tools_module = import_runtime_module("copilot_telegram.custom_tools")
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

    async def test_session_config_registers_custom_tools(self) -> None:
        client = FakeClient()
        manager = self.CopilotSessionManager(
            client=client,
            model="gpt-5",
            timeout_seconds=5,
        )

        await manager.ask(chat_id=12, prompt="hello")
        tools = client.create_session_configs[0].get("tools")
        self.assertIsInstance(tools, list)
        self.assertTrue(tools)
        self.assertEqual(
            [tool.name for tool in tools],
            [custom_tools_module.DOWNLOAD_BINARY_TOOL_NAME],
        )
        await manager.shutdown()


class CopilotSessionManagerToolGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.CopilotSessionManager = session_module.CopilotSessionManager

    async def test_skill_tool_is_denied_after_cap_for_same_session(self) -> None:
        manager = self.CopilotSessionManager(
            client=FakeClient(),
            model="gpt-5",
            timeout_seconds=5,
            skill_tool_max_calls_per_ask=2,
        )
        invocation = {"session_id": "s1"}
        input_data = {"toolName": "skill", "toolArgs": {"name": "weather-forecast"}}

        first = await manager._on_pre_tool_use(input_data, invocation)
        second = await manager._on_pre_tool_use(input_data, invocation)
        third = await manager._on_pre_tool_use(input_data, invocation)

        self.assertEqual(first["permissionDecision"], "allow")
        self.assertEqual(second["permissionDecision"], "allow")
        self.assertEqual(third["permissionDecision"], "deny")
        self.assertEqual(third["modifiedArgs"], {"name": "weather-forecast"})

    async def test_skill_tool_counter_is_isolated_per_session(self) -> None:
        manager = self.CopilotSessionManager(
            client=FakeClient(),
            model="gpt-5",
            timeout_seconds=5,
            skill_tool_max_calls_per_ask=1,
        )

        denied = await manager._on_pre_tool_use(
            {"toolName": "skill", "toolArgs": {}},
            {"session_id": "s-a"},
        )
        self.assertEqual(denied["permissionDecision"], "allow")
        denied = await manager._on_pre_tool_use(
            {"toolName": "skill", "toolArgs": {}},
            {"session_id": "s-a"},
        )
        self.assertEqual(denied["permissionDecision"], "deny")

        allowed_other = await manager._on_pre_tool_use(
            {"toolName": "skill", "toolArgs": {}},
            {"session_id": "s-b"},
        )
        self.assertEqual(allowed_other["permissionDecision"], "allow")

    async def test_non_skill_tool_is_not_capped(self) -> None:
        manager = self.CopilotSessionManager(
            client=FakeClient(),
            model="gpt-5",
            timeout_seconds=5,
            skill_tool_max_calls_per_ask=1,
        )
        invocation = {"session_id": "s1"}

        first = await manager._on_pre_tool_use(
            {"toolName": "report_intent", "toolArgs": {"x": 1}},
            invocation,
        )
        second = await manager._on_pre_tool_use(
            {"toolName": "report_intent", "toolArgs": {"x": 2}},
            invocation,
        )

        self.assertEqual(first["permissionDecision"], "allow")
        self.assertEqual(second["permissionDecision"], "allow")

    async def test_skill_tool_counter_can_reset_for_new_ask(self) -> None:
        manager = self.CopilotSessionManager(
            client=FakeClient(),
            model="gpt-5",
            timeout_seconds=5,
            skill_tool_max_calls_per_ask=1,
        )
        invocation = {"session_id": "s-reset"}
        input_data = {"toolName": "skill", "toolArgs": {}}

        await manager._on_pre_tool_use(input_data, invocation)
        denied = await manager._on_pre_tool_use(input_data, invocation)
        self.assertEqual(denied["permissionDecision"], "deny")

        manager._tool_call_counts_by_session["s-reset"] = {}
        allowed_after_reset = await manager._on_pre_tool_use(input_data, invocation)
        self.assertEqual(allowed_after_reset["permissionDecision"], "allow")


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

    def test_load_dispatcher_config_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = config_module.load_dispatcher_config()

        self.assertEqual(
            config.user_input_timeout_seconds,
            constants_module.DEFAULT_USER_INPUT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            config.shutdown_drain_timeout_seconds,
            constants_module.DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
        )

    def test_load_dispatcher_config_parses_shutdown_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "COPILOT_USER_INPUT_TIMEOUT_SECONDS": "111",
                "TELEGRAM_SHUTDOWN_DRAIN_TIMEOUT_SECONDS": "42",
            },
            clear=True,
        ):
            config = config_module.load_dispatcher_config()

        self.assertEqual(config.user_input_timeout_seconds, 111)
        self.assertEqual(config.shutdown_drain_timeout_seconds, 42)

    def test_load_startup_config_sets_binary_download_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_API_KEY": "token"},
            clear=True,
        ):
            config = config_module.load_startup_config()

        self.assertEqual(
            config.binary_download_max_bytes,
            constants_module.DEFAULT_BINARY_DOWNLOAD_MAX_BYTES,
        )
        self.assertEqual(
            config.binary_download_timeout_seconds,
            constants_module.DEFAULT_BINARY_DOWNLOAD_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            config.skill_tool_max_calls_per_ask,
            constants_module.DEFAULT_SKILL_TOOL_MAX_CALLS_PER_ASK,
        )

    def test_load_startup_config_parses_binary_download_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_API_KEY": "token",
                "COPILOT_BINARY_DOWNLOAD_MAX_BYTES": "1234",
                "COPILOT_BINARY_DOWNLOAD_TIMEOUT_SECONDS": "45",
                "COPILOT_SKILL_TOOL_MAX_CALLS_PER_ASK": "7",
            },
            clear=True,
        ):
            config = config_module.load_startup_config()

        self.assertEqual(config.binary_download_max_bytes, 1234)
        self.assertEqual(config.binary_download_timeout_seconds, 45)
        self.assertEqual(config.skill_tool_max_calls_per_ask, 7)


if __name__ == "__main__":
    unittest.main()
