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
  - This also bounds practical file size for tool-driven artifact workflows before Telegram delivery.

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

- `TELEGRAM_MAX_ARTIFACTS` (default: `8`)
  - Maximum artifacts sent for each request.

- `TELEGRAM_MAX_ARTIFACT_BYTES` (default: `47185920`)
  - Max file size in bytes (45 MiB) for an artifact to be sent.
  - Applies to both explicit tool-registered artifacts (`register_artifact_for_delivery`) and fallback artifact path extraction.

- `TELEGRAM_ARTIFACT_SEND_TIMEOUT_SECONDS` (default: `180`)
  - Per-attempt Telegram upload timeout (connect/read/write/pool) for artifact sends.
  - Large document uploads may require higher values than image uploads.

- `TELEGRAM_ARTIFACT_SEND_RETRIES` (default: `1`)
  - Number of retry attempts when artifact upload times out.
  - Total attempts are `1 + TELEGRAM_ARTIFACT_SEND_RETRIES`.

- `TELEGRAM_ARTIFACT_TEMP_ROOT` (default: `<system temp>/copilot-telegram-artifacts`)
  - Managed temp root used to stage eligible external artifacts before sending to Telegram.

- `TELEGRAM_ARTIFACT_ALLOW_TMP_SOURCES_ONLY` (default: `true`)
  - When `true`, only external artifact source files under the OS temp directory are eligible for staging/sending.
  - When `false`, any readable external source path may be staged (less restrictive).

- `TELEGRAM_ARTIFACT_REQUIRE_EXPLICIT_INTENT` (default: `true`)
  - When `true`, artifact delivery only uses explicit `register_artifact_for_delivery` intents.
  - When `false`, dispatcher also considers implicit `artifact_paths` and `external_artifact_paths` fallback candidates.

- `TELEGRAM_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` (default: `60`)
  - Maximum time to drain queued and in-flight chat work on shutdown before remaining workers are cancelled.

## Invalid values

Integer variables fall back to defaults if parsing fails. Invalid `COPILOT_LOG_LEVEL` values fall back to `info`. Invalid `COPILOT_REASONING_EFFORT` values are ignored.
