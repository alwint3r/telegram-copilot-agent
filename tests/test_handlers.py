from __future__ import annotations

import types
import unittest

from tests.support.module_loader import import_runtime_module

handlers_module = import_runtime_module("copilot_telegram.handlers")


class ApplicationErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_error_handler_logs_network_error_as_warning(self) -> None:
        context = types.SimpleNamespace(
            error=handlers_module.NetworkError("Server disconnected")
        )
        with self.assertLogs("copilot_telegram.handlers", level="WARNING") as logs:
            await handlers_module.application_error_handler(None, context)
        self.assertTrue(
            any("Telegram network error in application handler" in line for line in logs.output)
        )

    async def test_application_error_handler_logs_unhandled_error_with_traceback(self) -> None:
        context = types.SimpleNamespace(error=RuntimeError("boom"))
        with self.assertLogs("copilot_telegram.handlers", level="ERROR") as logs:
            await handlers_module.application_error_handler(None, context)
        self.assertTrue(
            any("Unhandled Telegram application error" in line for line in logs.output)
        )


if __name__ == "__main__":
    unittest.main()
