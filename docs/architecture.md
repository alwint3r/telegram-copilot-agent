# Architecture

## Runtime overview

The runtime is now organized as the `copilot_telegram` package:

- `copilot_telegram/session_manager.py`: `CopilotSessionManager` owns one Copilot SDK session per Telegram chat, serializes prompt execution with per-session locks, and handles SDK hooks.
- `copilot_telegram/dispatcher.py`: `BackgroundDispatcher` queues incoming prompts per chat, runs worker tasks, streams progress, sends replies, and uploads eligible artifacts.
- `copilot_telegram/handlers.py`: Telegram command/message handlers plus startup/shutdown callbacks.
- `copilot_telegram/config.py`: environment-driven startup and dispatcher config parsing.
- `copilot_telegram/artifacts.py`, `copilot_telegram/text_utils.py`, `copilot_telegram/user_input.py`: focused helper modules.
- `copilot_telegram/cli.py`: application wiring and polling entrypoint.

`chat-telegram.py` is a thin executable wrapper that calls `copilot_telegram.cli.main()`.

## Request lifecycle

1. A Telegram text message arrives in `message_handler`.
2. `BackgroundDispatcher.enqueue` assigns a request id and pushes a `PromptItem` to that chat's queue.
3. `_chat_worker` processes queued items in FIFO order for the chat.
4. `CopilotSessionManager.ask` sends the prompt and streams events through `AskEventCollector`.
5. `WorkerProgressReporter` throttles progress edits to a single status message.
6. Final assistant output is split to Telegram-safe chunks and sent.
7. Artifact candidates from stream metadata (`AskResult.artifact_paths`) are filtered and sent.

## Session model

- Session key: Telegram `chat_id`.
- Session concurrency: one active ask per session lock.
- Session reset: `/reset` marks state closed, waits for in-flight work to release lock, then destroys the session.
- Shutdown: first SIGINT/SIGTERM triggers graceful drain (bounded timeout), second signal escalates to forced worker cancellation.

## Artifact model

- Artifact paths are collected from SDK stream event payloads (direct paths, attachments, and nested event object values).
- Only in-workspace files are eligible.
- Files are sent only when `is_sendable_artifact` accepts type and size constraints.
- Dispatcher sends at most `TELEGRAM_MAX_ARTIFACTS` artifacts per request.

## Semantic compression guidelines

This codebase follows a conservative semantic compression style:

- Name domain concepts explicitly (`AskEventCollector`, `WorkerProgressReporter`, `PromptItem`).
- Keep control flow linear at call sites and push detail into focused helpers.
- Prefer a few high-signal abstractions over many thin wrappers.
- Preserve behavior while reducing incidental branching and duplicated state handling.
