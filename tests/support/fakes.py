"""Fake implementations used by async runtime tests."""

from __future__ import annotations

import asyncio
import types
from typing import Any


class FakeSession:
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
        self._handlers: list[Any] = []

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


class FakeClient:
    def __init__(self) -> None:
        self.created_sessions: list[FakeSession] = []
        self.create_session_configs: list[dict[str, object]] = []
        self._counter = 0
        self.first_session_send_gate: asyncio.Event | None = None
        self.first_session_terminal_error: str | None = None
        self.models_rejecting_reasoning_effort: set[str] = set()

    async def create_session(self, config: dict[str, object]) -> FakeSession:
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
        session = FakeSession(
            session_id=session_id,
            send_gate=gate,
            terminal_error=terminal_error,
        )
        self.created_sessions.append(session)
        return session


class FakeBot:
    def __init__(self) -> None:
        self._message_id = 0
        self.sent_messages: list[dict[str, object]] = []
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


class FakeApplication:
    def __init__(self) -> None:
        self.bot = FakeBot()
        self.bot_data: dict[str, object] = {}


class GatedAskManager:
    def __init__(self, ask_result_type: Any) -> None:
        self._ask_result_type = ask_result_type
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
        return self._ask_result_type(reply=f"reply:{prompt}")


class StaticAskManager:
    def __init__(self, ask_result_type: Any, ask_result=None) -> None:
        self._ask_result_type = ask_result_type
        self.prompts: list[str] = []
        self.ask_result = ask_result

    async def ask(self, chat_id: int, prompt: str, progress_callback=None):
        _ = chat_id, progress_callback
        self.prompts.append(prompt)
        if self.ask_result is not None:
            return self.ask_result
        return self._ask_result_type(reply=f"reply:{prompt}")
