from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.module_loader import import_runtime_module

artifacts_module = import_runtime_module("copilot_telegram.artifacts")
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

    def test_build_result_keeps_artifacts_when_reply_missing(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "artifact.bin")
            artifact.write_bytes(b"artifact")

            collector = session_module.AskEventCollector(workspace_root=workspace)
            collector.collect_path(str(artifact))
            result = collector.build_result()

            self.assertEqual(result.reply, "I could not generate a response.")
            self.assertEqual(result.artifact_paths, [str(artifact.resolve())])

    def test_collect_path_keeps_in_workspace_candidate_before_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            artifact = Path(workspace, "later.bin")
            self.assertFalse(artifact.exists())

            collector = session_module.AskEventCollector(workspace_root=workspace)
            collector.collect_path(str(artifact))
            result = collector.build_result()

            self.assertEqual(result.artifact_paths, [str(artifact.resolve())])

    def test_normalize_workspace_path_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            inside = Path(workspace, "inside.txt")
            inside.write_text("ok", encoding="utf-8")

            normalized_inside = artifacts_module.normalize_workspace_path(
                "inside.txt", workspace
            )
            escaped = artifacts_module.normalize_workspace_path("../outside.txt", workspace)

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

            found = artifacts_module.extract_existing_paths_from_obj(payload, workspace)
            self.assertEqual(found, {str(artifact.resolve())})

    def test_is_sendable_artifact_filters_text_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            image_path = Path(workspace, "plot.png")
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            text_path = Path(workspace, "notes.txt")
            text_path.write_text("hello", encoding="utf-8")

            self.assertTrue(
                artifacts_module.is_sendable_artifact(
                    str(image_path), max_artifact_bytes=1024
                )
            )
            self.assertFalse(
                artifacts_module.is_sendable_artifact(
                    str(text_path), max_artifact_bytes=1024
                )
            )
            self.assertFalse(
                artifacts_module.is_sendable_artifact(str(image_path), max_artifact_bytes=1)
            )

    def test_default_user_input_answer_prefers_negative_choice(self) -> None:
        answer = user_input_module.default_user_input_answer(
            choices=["Yes", "No", "Skip"], allow_freeform=False
        )
        self.assertEqual(answer, {"answer": "No", "wasFreeform": False})


if __name__ == "__main__":
    unittest.main()
