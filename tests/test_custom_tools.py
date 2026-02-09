from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from tests.support.module_loader import import_runtime_module

custom_tools_module = import_runtime_module("copilot_telegram.custom_tools")


class FakeResponse:
    def __init__(
        self,
        *,
        chunks: list[bytes],
        status_error: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    async def aiter_bytes(self, chunk_size: int = 65536):
        _ = chunk_size
        for chunk in self._chunks:
            yield chunk


class FakeStreamContext:
    def __init__(self, *, response: FakeResponse, enter_error: Exception | None) -> None:
        self._response = response
        self._enter_error = enter_error

    async def __aenter__(self) -> FakeResponse:
        if self._enter_error is not None:
            raise self._enter_error
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _ = exc_type, exc, tb


class FakeAsyncClient:
    chunks: list[bytes] = []
    status_error: Exception | None = None
    enter_error: Exception | None = None

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _ = exc_type, exc, tb

    def stream(self, method: str, url: str) -> FakeStreamContext:
        _ = method, url
        return FakeStreamContext(
            response=FakeResponse(
                chunks=list(self.chunks),
                status_error=self.status_error,
            ),
            enter_error=self.enter_error,
        )


class BinaryDownloadToolTests(unittest.IsolatedAsyncioTestCase):
    def _build_handler(
        self,
        *,
        workspace: str,
        tool_name: str,
        max_bytes: int = 1024,
    ):
        tools = custom_tools_module.build_custom_tools(
            working_directory=workspace,
            max_download_bytes=max_bytes,
            download_timeout_seconds=10,
            max_artifact_bytes=max_bytes,
        )
        tool = next((item for item in tools if item.name == tool_name), None)
        self.assertIsNotNone(tool)
        return tool.handler

    async def test_download_binary_success_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.DOWNLOAD_BINARY_TOOL_NAME,
            )
            FakeAsyncClient.chunks = [b"\x00\x01", b"\x02"]
            FakeAsyncClient.status_error = None
            FakeAsyncClient.enter_error = None

            with patch.object(custom_tools_module.httpx, "AsyncClient", FakeAsyncClient):
                result = await handler(
                    {
                        "arguments": {
                            "url": "https://example.com/file.bin",
                            "output_dir": "downloads",
                        }
                    }
                )

            self.assertEqual(result["resultType"], "success")
            output_path = Path(result["textResultForLlm"])
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_bytes(), b"\x00\x01\x02")

    async def test_download_binary_success_in_system_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            with tempfile.TemporaryDirectory() as temp_output:
                handler = self._build_handler(
                    workspace=workspace,
                    tool_name=custom_tools_module.DOWNLOAD_BINARY_TOOL_NAME,
                )
                FakeAsyncClient.chunks = [b"abc"]
                FakeAsyncClient.status_error = None
                FakeAsyncClient.enter_error = None

                with patch.object(
                    custom_tools_module.httpx, "AsyncClient", FakeAsyncClient
                ):
                    result = await handler(
                        {
                            "arguments": {
                                "url": "https://example.com/data.bin",
                                "output_dir": temp_output,
                            }
                        }
                    )

                self.assertEqual(result["resultType"], "success")
                self.assertTrue(
                    Path(result["textResultForLlm"]).is_relative_to(
                        Path(os.path.realpath(temp_output))
                    )
                )

    async def test_download_binary_rejects_non_http_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.DOWNLOAD_BINARY_TOOL_NAME,
            )
            result = await handler(
                {
                    "arguments": {
                        "url": "file:///tmp/a.bin",
                        "output_dir": "downloads",
                    }
                }
            )
            self.assertEqual(result["resultType"], "failure")
            self.assertIn("http and https", result["error"])

    async def test_download_binary_rejects_output_dir_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.DOWNLOAD_BINARY_TOOL_NAME,
            )
            result = await handler(
                {
                    "arguments": {
                        "url": "https://example.com/file.bin",
                        "output_dir": os.getcwd(),
                    }
                }
            )
            self.assertEqual(result["resultType"], "failure")
            self.assertIn("workspace or system temp", result["error"])

    async def test_download_binary_enforces_size_limit_and_cleans_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.DOWNLOAD_BINARY_TOOL_NAME,
                max_bytes=3,
            )
            FakeAsyncClient.chunks = [b"ab", b"cd"]
            FakeAsyncClient.status_error = None
            FakeAsyncClient.enter_error = None

            with patch.object(custom_tools_module.httpx, "AsyncClient", FakeAsyncClient):
                result = await handler(
                    {
                        "arguments": {
                            "url": "https://example.com/file.bin",
                            "output_dir": "downloads",
                        }
                    }
                )

            self.assertEqual(result["resultType"], "failure")
            self.assertIn("exceeds", result["error"])
            self.assertFalse(Path(workspace, "downloads", "file.bin").exists())

    async def test_download_binary_handles_timeout_failure(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.DOWNLOAD_BINARY_TOOL_NAME,
            )
            FakeAsyncClient.chunks = []
            FakeAsyncClient.status_error = None
            FakeAsyncClient.enter_error = httpx.TimeoutException("timeout")

            with patch.object(custom_tools_module.httpx, "AsyncClient", FakeAsyncClient):
                result = await handler(
                    {
                        "arguments": {
                            "url": "https://example.com/file.bin",
                            "output_dir": "downloads",
                        }
                    }
                )

            self.assertEqual(result["resultType"], "failure")
            self.assertIn("timeout", result["error"])

    async def test_register_artifact_success(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.REGISTER_ARTIFACT_TOOL_NAME,
            )
            artifact = Path(workspace, "chart.png")
            artifact.write_bytes(b"\x89PNG\r\n\x1a\n")

            result = await handler(
                {"arguments": {"path": str(artifact), "caption": "Weather chart"}}
            )

            self.assertEqual(result["resultType"], "success")
            payload = result["textResultForLlm"]
            self.assertIn("\"delivery_intent\":true", payload)
            self.assertIn(str(artifact.resolve()), payload)
            self.assertIn("Weather chart", payload)

    async def test_register_artifact_rejects_non_sendable_file(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.REGISTER_ARTIFACT_TOOL_NAME,
            )
            artifact = Path(workspace, "notes.txt")
            artifact.write_text("hello", encoding="utf-8")

            result = await handler({"arguments": {"path": str(artifact)}})

            self.assertEqual(result["resultType"], "failure")
            self.assertIn("eligible sendable artifact", result["error"])

    async def test_register_artifact_rejects_path_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._build_handler(
                workspace=workspace,
                tool_name=custom_tools_module.REGISTER_ARTIFACT_TOOL_NAME,
            )
            outside_artifact = Path(os.getcwd(), "outside.zip")
            outside_artifact.write_bytes(b"test")
            try:
                result = await handler({"arguments": {"path": str(outside_artifact)}})
            finally:
                outside_artifact.unlink(missing_ok=True)

            self.assertEqual(result["resultType"], "failure")
            self.assertIn("workspace or system temp", result["error"])


if __name__ == "__main__":
    unittest.main()
