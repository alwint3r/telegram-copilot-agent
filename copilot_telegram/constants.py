"""Shared constants for Telegram/Copilot runtime behavior."""

DEFAULT_USER_INPUT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_ARTIFACTS_PER_REQUEST = 8
DEFAULT_MAX_ARTIFACT_BYTES = 45 * 1024 * 1024
DEFAULT_BINARY_DOWNLOAD_MAX_BYTES = 45 * 1024 * 1024
DEFAULT_BINARY_DOWNLOAD_TIMEOUT_SECONDS = 120
DEFAULT_ARTIFACT_SEND_TIMEOUT_SECONDS = 180
DEFAULT_ARTIFACT_SEND_RETRIES = 1
DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 60
DEFAULT_ARTIFACT_TEMP_ROOT_BASENAME = "copilot-telegram-artifacts"
DEFAULT_ARTIFACT_ALLOW_TMP_SOURCES_ONLY = True
DEFAULT_ARTIFACT_REQUIRE_EXPLICIT_INTENT = True
DEFAULT_SKILL_TOOL_MAX_CALLS_PER_ASK = 4
HEARTBEAT_INTERVAL_SECONDS = 15
PROGRESS_EDIT_THROTTLE_SECONDS = 1.2
PROGRESS_PREVIEW_CHARS = 1200
VALID_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}

IGNORED_SCAN_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}
TEXTUAL_ARTIFACT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
    ".svg",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".sql",
    ".log",
}
DIRECT_SEND_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".zip",
}
