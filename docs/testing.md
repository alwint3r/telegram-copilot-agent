# Testing

## Run tests

```bash
python -m unittest -v
```

## Current focus areas

- Session reset behavior and lock coordination.
- Retry behavior when an ask encounters a session closed before lock acquisition.
- Utility behavior for message splitting, workspace path normalization, artifact filtering, and default user-input answers.

## Test design notes

- Tests load `chat-telegram.py` through module stubs so they run without real Copilot SDK or Telegram network calls.
- Session tests use fake client/session objects to simulate streaming and lifecycle events.
- Utility tests use temporary directories and files to validate filesystem-driven helpers.
