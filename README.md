# Copilot Telegram Bridge (Python)

This project runs a Telegram bot that forwards user messages to the GitHub Copilot SDK, keeps per-chat Copilot sessions, streams progress updates, and sends generated artifacts back to the chat when possible.

## What is in this repo

- `chat-telegram.py`: main Telegram bot runtime with session management and background dispatch.
- `chat-inline.py`: minimal local CLI loop for direct Copilot chat testing.
- `test_copilot_session_manager.py`: unit tests for session reset and concurrency behavior.
- `.github/skills/*/SKILL.md`: prompt-execution skill docs for weather and plotting tasks.

## Quick start

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Configure environment variables (see `docs/configuration.md`).
4. Start the Telegram bot:

```bash
python chat-telegram.py
```

## Bot behavior

- `/start`: prints a short usage message.
- `/reset`: destroys the current Copilot session for the chat.
- Text message: queued and processed in background order per chat.

## Development

- Run tests:

```bash
python -m unittest -v
```

- Architecture details: `docs/architecture.md`
- Configuration reference: `docs/configuration.md`
- Testing notes: `docs/testing.md`
