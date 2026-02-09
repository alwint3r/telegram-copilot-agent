# Architecture

## Runtime overview

The runtime is now organized as the `copilot_telegram` package:

- `copilot_telegram/session_manager.py`: `CopilotSessionManager` owns one Copilot SDK session per Telegram chat, serializes prompt execution with per-session locks, and handles SDK hooks.
- `copilot_telegram/custom_tools.py`: custom Copilot SDK tools (`download_binary_file` and `register_artifact_for_delivery`).
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
5. Copilot may call custom SDK tools during execution (for example binary URL download to local file, then explicit artifact registration).
6. `WorkerProgressReporter` throttles progress edits to a single status message.
7. Final assistant output is split to Telegram-safe chunks and sent.
8. Artifact candidates come from explicit tool delivery intents (`AskResult.artifact_intents`) by default; implicit fallback paths are used only when `TELEGRAM_ARTIFACT_REQUIRE_EXPLICIT_INTENT=false`.
9. Dispatcher sends artifact delivery outcome summaries when uploads fail so users can see what happened.

## Session model

- Session key: Telegram `chat_id`.
- Session concurrency: one active ask per session lock.
- Session reset: `/reset` marks state closed, waits for in-flight work to release lock, then destroys the session.
- Shutdown: first SIGINT/SIGTERM triggers graceful drain (bounded timeout), second signal escalates to forced worker cancellation.

## Artifact model

- Artifact intents are collected from explicit custom tool payloads first (for deterministic delivery signaling).
- By default, artifact intent collection is strict: only explicit `register_artifact_for_delivery` intents are collected from stream payloads.
- Optional fallback extraction from generic stream payload paths can be re-enabled through `TELEGRAM_ARTIFACT_REQUIRE_EXPLICIT_INTENT=false`.
- In-workspace files are sent directly.
- Existing external files are tracked separately and staged into a bot-managed temp directory before sending.
- By default, only external source files under the OS temp directory are eligible for staging.
- Files are sent only when `is_sendable_artifact` accepts type and size constraints.
- Dispatcher sends at most `TELEGRAM_MAX_ARTIFACTS` artifacts per request.
- Dispatcher applies configured timeout and retry logic for artifact upload timeouts.
- If uploads fail, dispatcher posts a Telegram summary message with failure details.

## Tool-call safeguards

- `CopilotSessionManager` enforces a per-ask cap on repeated `skill` tool calls.
- When the cap is exceeded, subsequent `skill` calls are denied so execution can continue without unbounded loop behavior.

## Semantic compression guidelines

This codebase follows a conservative semantic compression style:

- Name domain concepts explicitly (`AskEventCollector`, `WorkerProgressReporter`, `PromptItem`).
- Keep control flow linear at call sites and push detail into focused helpers.
- Prefer a few high-signal abstractions over many thin wrappers.
- Preserve behavior while reducing incidental branching and duplicated state handling.
