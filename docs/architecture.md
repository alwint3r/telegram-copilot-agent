# Architecture

## Runtime overview

The runtime in `chat-telegram.py` has two primary components:

- `CopilotSessionManager`: owns one Copilot SDK session per Telegram chat, serializes prompt execution with per-session locks, and handles SDK hooks.
- `BackgroundDispatcher`: queues incoming prompts per chat, runs worker tasks, streams progress, sends final text replies, and uploads eligible artifacts.

## Request lifecycle

1. A Telegram text message arrives in `message_handler`.
2. `BackgroundDispatcher.enqueue` assigns a request id and pushes a `PromptItem` to that chat's queue.
3. `_chat_worker` processes queued items in FIFO order for the chat.
4. `CopilotSessionManager.ask` sends the prompt to Copilot and streams events through `AskEventCollector`.
5. Progress updates are throttled via `WorkerProgressReporter` and sent by editing a single status message.
6. Final assistant response is split to Telegram-safe chunks and posted.
7. Artifact candidates are gathered from stream metadata and workspace file changes, then filtered and sent.

## Session model

- Session key: Telegram `chat_id`.
- Session concurrency: one active ask per session lock.
- Session reset: `/reset` marks the state closed, waits for in-flight work to release lock, and destroys the session.
- Shutdown: SIGINT/SIGTERM first trigger graceful drain of queued and in-flight work (bounded timeout), then session teardown; a second signal escalates to forced worker cancellation.

## Artifact model

- Workspace snapshots are taken before ask execution.
- New/modified files are detected after ask completion.
- Files are sent only when `is_sendable_artifact` accepts type and size constraints.

## Semantic compression guidelines

This codebase follows a conservative semantic compression style:

- Name domain concepts explicitly (`AskEventCollector`, `WorkerProgressReporter`, `PromptItem`).
- Keep control flow linear at call sites (`send -> wait -> return`) and push detail into focused helpers.
- Prefer a few high-signal abstractions over many thin wrappers.
- Preserve behavior while reducing incidental branching and duplicated state handling.
