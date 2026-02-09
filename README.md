# Copilot Telegram Bridge (Python)

This project runs a Telegram bot that forwards user messages to the GitHub Copilot SDK, keeps per-chat Copilot sessions, streams progress updates, and sends generated artifacts back to the chat when possible.

## What is in this repo

- `chat-telegram.py`: thin executable wrapper that runs the bot CLI.
- `copilot_telegram/`: main runtime package.
  - `cli.py`: app wiring and startup.
  - `handlers.py`: Telegram command/message handlers.
  - `session_manager.py`: per-chat Copilot session lifecycle.
  - `dispatcher.py`: background queue workers and artifact sending.
  - `config.py`, `models.py`, `artifacts.py`, `text_utils.py`, `user_input.py`: shared runtime helpers.
- `chat-inline.py`: minimal local CLI loop for direct Copilot chat testing.
- `tests/`: unit and async integration-style tests.
- `docs/`: architecture, configuration, and testing documentation.

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

## Where to edit

- Add or change Telegram command behavior: `copilot_telegram/handlers.py`
- Change Copilot session policy/timeouts/reasoning behavior: `copilot_telegram/session_manager.py` and `copilot_telegram/config.py`
- Change queue/progress/artifact behavior: `copilot_telegram/dispatcher.py`
