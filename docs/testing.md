# Testing

## Run tests

```bash
uv run python -m unittest -v
```

## Test layout

- `tests/test_session_manager.py`: session reset/lifecycle behavior, reasoning-effort config behavior, and tool-call guard coverage.
- `tests/test_custom_tools.py`: custom tool behavior (`download_binary_file`, `register_artifact_for_delivery`) including success/failure guardrails.
- `tests/test_dispatcher.py`: dispatcher shutdown, queue concurrency, artifact sending behavior, and user-visible artifact failure summaries.
- `tests/test_utils.py`: utility coverage for text splitting, path normalization, artifact filtering, and user-input defaults.
- `tests/support/fakes.py`: fake client/session/bot/application implementations used by async tests.
- `tests/support/module_loader.py`: dependency stubs for Copilot/Telegram imports.

## Current focus areas

- Session reset behavior and lock coordination.
- Retry behavior when an ask encounters a session closed before lock acquisition.
- Repeated `skill` tool-call guard behavior (deny after per-ask cap).
- Utility behavior for message splitting, workspace path normalization, artifact filtering, and default user-input answers.

## Test design notes

- Tests run without real Copilot SDK or Telegram network calls by installing module stubs.
- Session tests use fake client/session objects to simulate streaming and lifecycle events.
- Utility tests use temporary directories and files to validate filesystem-driven helpers.
