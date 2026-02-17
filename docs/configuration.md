# Configuration

## Required

- `TELEGRAM_BOT_API_KEY`: Telegram bot API token.

## Optional

- `COPILOT_MODEL` (default: `gpt-5`)
  - Copilot model name used when creating sessions.

- `COPILOT_REASONING_EFFORT`
  - Optional reasoning effort for models that support it.
  - Allowed values: `low`, `medium`, `high`, `xhigh`.
  - Applied only to `gpt-5` and newer `gpt-*` model names.
  - If a model rejects this option at runtime, the bot retries session creation without it.

- `COPILOT_TIMEOUT_SECONDS` (default: `600`)
  - Max seconds to wait for a Copilot request before timing out.

- `COPILOT_BINARY_DOWNLOAD_MAX_BYTES` (default: `47185920`)
  - Max bytes the custom `download_binary_file` tool can download in one call.

- `COPILOT_BINARY_DOWNLOAD_TIMEOUT_SECONDS` (default: `120`)
  - Timeout in seconds for one custom `download_binary_file` tool request.

- `COPILOT_SKILL_TOOL_MAX_CALLS_PER_ASK` (default: `4`)
  - Maximum number of `skill` tool calls allowed during one Copilot ask before further `skill` calls are denied.
  - Helps prevent runaway tool-loop behavior.

- `COPILOT_LOG_LEVEL` (default: `info`)
  - One of: `none`, `error`, `warning`, `info`, `debug`, `all`.

- `COPILOT_USER_INPUT_TIMEOUT_SECONDS` (default: `300`)
  - Timeout for user replies when Copilot requests additional input.

- `COPILOT_USER_INPUT_DEFAULT_ANSWER`
  - Default fallback free-form answer when no choices are provided.
  - If omitted, uses: `Proceed using your best judgment and continue.`

- `TELEGRAM_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` (default: `60`)
  - Maximum time to drain queued and in-flight chat work on shutdown before remaining workers are cancelled.

## Invalid values

Integer variables fall back to defaults if parsing fails. Invalid `COPILOT_LOG_LEVEL` values fall back to `info`. Invalid `COPILOT_REASONING_EFFORT` values are ignored.

## Raspberry Pi deployment notes

- The Raspberry Pi 4 `systemd` deployment flow uses `<repo>/runtime/copilot-telegram.env` as the environment file location (for example `/opt/copilot-telegram/runtime/copilot-telegram.env`).
- The service is designed to run with `uv` (`uv run chat-telegram.py ...`) and does not support Python interpreter fallback.
- See `docs/deployment-raspberry-pi.md` for end-to-end installation and operations steps.
