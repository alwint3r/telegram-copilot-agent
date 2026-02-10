"""Helpers to import runtime modules with lightweight dependency stubs."""

from __future__ import annotations

import importlib
import sys
import types


def install_dependency_stubs() -> None:
    """Install stubs for external dependencies used by runtime modules."""

    copilot_module = types.ModuleType("copilot")
    copilot_module.CopilotClient = object
    sys.modules["copilot"] = copilot_module

    copilot_types_module = types.ModuleType("copilot.types")

    class Tool:
        def __init__(
            self,
            name: str,
            description: str,
            handler,
            parameters: dict[str, object] | None = None,
        ) -> None:
            self.name = name
            self.description = description
            self.handler = handler
            self.parameters = parameters

    for alias in (
        "CopilotClientOptions",
        "LogLevel",
        "PreToolUseHookInput",
        "PreToolUseHookOutput",
        "SessionConfig",
        "ToolInvocation",
        "ToolResult",
        "UserInputRequest",
        "UserInputResponse",
    ):
        setattr(copilot_types_module, alias, dict)
    copilot_types_module.Tool = Tool
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

    class NetworkError(Exception):
        pass

    telegram_error.BadRequest = BadRequest
    telegram_error.NetworkError = NetworkError
    sys.modules["telegram.error"] = telegram_error

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.Application = object
    telegram_ext.CommandHandler = object
    telegram_ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    telegram_ext.MessageHandler = object
    telegram_ext.filters = types.SimpleNamespace(TEXT=1, COMMAND=2)
    sys.modules["telegram.ext"] = telegram_ext


def import_runtime_module(module_name: str):
    """Import one runtime module after ensuring external stubs are available."""

    install_dependency_stubs()
    return importlib.import_module(module_name)
