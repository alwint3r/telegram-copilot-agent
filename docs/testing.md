# Testing

## Run tests

```bash
uv run python -m unittest -v
```

## Test layout

- `tests/test_session_manager.py`: session reset/lifecycle behavior, reasoning-effort config behavior, and tool-call guard coverage.
- `tests/test_custom_tools.py`: custom tool behavior (`download_binary_file`) including success/failure guardrails.
- `tests/test_dispatcher.py`: dispatcher shutdown, queue concurrency, worker text-reply behavior, and user-input reply handling.
- `tests/test_handlers.py`: global Telegram application error-handler behavior.
- `tests/test_utils.py`: utility coverage for text splitting, stream collector behavior, and user-input defaults.
- `tests/support/fakes.py`: fake client/session/bot/application implementations used by async tests.
- `tests/support/module_loader.py`: dependency stubs for Copilot/Telegram imports.

## Current focus areas

- Session reset behavior and lock coordination.
- Retry behavior when an ask encounters a session closed before lock acquisition.
- Repeated `skill` tool-call guard behavior (deny after per-ask cap).
- Dispatcher queue ordering, failure handling, and user-input reply flow.
- Utility behavior for message splitting and default user-input answers.

## Test design notes

- Tests run without real Copilot SDK or Telegram network calls by installing module stubs.
- Session tests use fake client/session objects to simulate streaming and lifecycle events.
- Utility tests use temporary directories and synthetic events to validate helper logic.
