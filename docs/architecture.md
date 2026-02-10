# Architecture

## Runtime overview

The runtime is organized as the `copilot_telegram` package:

- `copilot_telegram/session_manager.py`: `CopilotSessionManager` owns one Copilot SDK session per Telegram chat, serializes prompt execution with per-session locks, and handles SDK hooks.
- `copilot_telegram/custom_tools.py`: custom Copilot SDK tools (`download_binary_file`).
- `copilot_telegram/dispatcher.py`: `BackgroundDispatcher` queues incoming prompts per chat, runs worker tasks, streams progress, and sends reply text.
- `copilot_telegram/handlers.py`: Telegram command/message handlers plus startup/shutdown callbacks.
- `copilot_telegram/config.py`: environment-driven startup and dispatcher config parsing.
- `copilot_telegram/text_utils.py`, `copilot_telegram/user_input.py`: focused helper modules.
- `copilot_telegram/cli.py`: application wiring and polling entrypoint.

`chat-telegram.py` is a thin executable wrapper that calls `copilot_telegram.cli.main()`.

## Request lifecycle

1. A Telegram text message arrives in `message_handler`.
2. `BackgroundDispatcher.enqueue` assigns a request id and pushes a `PromptItem` to that chat's queue.
3. `_chat_worker` processes queued items in FIFO order for the chat.
4. `CopilotSessionManager.ask` sends the prompt and streams events through `AskEventCollector`.
5. Copilot may call custom SDK tools during execution (for example binary URL download to local file).
6. `WorkerProgressReporter` throttles progress edits to a single status message.
7. Final assistant output is split to Telegram-safe chunks and sent.

## Session model

- Session key: Telegram `chat_id`.
- Session concurrency: one active ask per session lock.
- Session reset: `/reset` marks state closed, waits for in-flight work to release lock, then destroys the session.
- Shutdown: first SIGINT/SIGTERM triggers graceful drain (bounded timeout), second signal escalates to forced worker cancellation.

## Error handling

- The Telegram `Application` registers a global async error handler.
- Transient Telegram network errors are logged as warnings.
- Unexpected framework-level exceptions are logged with traceback context to aid incident triage.

## Tool-call safeguards

- `CopilotSessionManager` enforces a per-ask cap on repeated `skill` tool calls.
- When the cap is exceeded, subsequent `skill` calls are denied so execution can continue without unbounded loop behavior.
