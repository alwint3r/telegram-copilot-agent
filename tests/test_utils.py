from __future__ import annotations

import tempfile
import types
import unittest

from tests.support.module_loader import import_runtime_module

session_module = import_runtime_module("copilot_telegram.session_manager")
text_utils_module = import_runtime_module("copilot_telegram.text_utils")
user_input_module = import_runtime_module("copilot_telegram.user_input")


class ChatTelegramUtilityTests(unittest.TestCase):
    def test_split_for_telegram_prefers_newline_boundaries(self) -> None:
        text = "alpha line\nbeta line\ngamma line"
        chunks = text_utils_module.split_for_telegram(text, chunk_size=12)
        self.assertEqual(chunks, ["alpha line\n", "beta line\n", "gamma line"])

    def test_split_for_telegram_preserves_whitespace_round_trip(self) -> None:
        text = "0123456789\n    indented line\n\nnext line"
        chunks = text_utils_module.split_for_telegram(text, chunk_size=14)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 14 for chunk in chunks))
        self.assertTrue(any(chunk.startswith("    ") for chunk in chunks))

    def test_build_result_uses_fallback_reply_when_empty(self) -> None:
        collector = session_module.AskEventCollector()
        result = collector.build_result()
        self.assertEqual(result.reply, "I could not generate a response.")

    def test_snapshot_uses_final_message_when_present(self) -> None:
        collector = session_module.AskEventCollector()
        collector.on_event(
            types.SimpleNamespace(
                type=types.SimpleNamespace(value="assistant.message_delta"),
                data=types.SimpleNamespace(delta_content="partial"),
            )
        )
        collector.on_event(
            types.SimpleNamespace(
                type=types.SimpleNamespace(value="assistant.message"),
                data=types.SimpleNamespace(content="final"),
            )
        )

        result = collector.build_result()
        self.assertEqual(result.reply, "final")

    def test_on_event_records_status_details(self) -> None:
        collector = session_module.AskEventCollector()
        collector.on_event(
            types.SimpleNamespace(
                type=types.SimpleNamespace(value="session.info"),
                data=types.SimpleNamespace(message="warming up"),
            )
        )

        statuses = collector.pop_pending_statuses()
        self.assertEqual(statuses, ["session.info: warming up"])

    def test_default_user_input_answer_prefers_negative_choice(self) -> None:
        answer = user_input_module.default_user_input_answer(
            choices=["Yes", "No", "Skip"], allow_freeform=False
        )
        self.assertEqual(answer, {"answer": "No", "wasFreeform": False})

    def test_default_user_input_answer_uses_env_default_for_freeform(self) -> None:
        with tempfile.TemporaryDirectory():
            answer = user_input_module.default_user_input_answer(
                choices=[], allow_freeform=True
            )
        self.assertTrue(answer["wasFreeform"])


if __name__ == "__main__":
    unittest.main()
