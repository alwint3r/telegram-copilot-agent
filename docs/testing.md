# Testing

## Run tests

```bash
python -m unittest -v
```

## Test layout

- `tests/test_session_manager.py`: session reset/lifecycle behavior and reasoning-effort config behavior.
- `tests/test_dispatcher.py`: dispatcher shutdown, queue concurrency, and artifact sending behavior.
- `tests/test_utils.py`: utility coverage for text splitting, path normalization, artifact filtering, and user-input defaults.
- `tests/support/fakes.py`: fake client/session/bot/application implementations used by async tests.
- `tests/support/module_loader.py`: dependency stubs for Copilot/Telegram imports.

## Current focus areas

- Session reset behavior and lock coordination.
- Retry behavior when an ask encounters a session closed before lock acquisition.
- Utility behavior for message splitting, workspace path normalization, artifact filtering, and default user-input answers.

## Test design notes

- Tests run without real Copilot SDK or Telegram network calls by installing module stubs.
- Session tests use fake client/session objects to simulate streaming and lifecycle events.
- Utility tests use temporary directories and files to validate filesystem-driven helpers.
