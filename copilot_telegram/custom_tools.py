"""Custom SDK tools used by Copilot sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from copilot.types import Tool

from .artifacts import is_sendable_artifact
from .constants import DEFAULT_MAX_ARTIFACT_BYTES

DOWNLOAD_BINARY_TOOL_NAME = "download_binary_file"
REGISTER_ARTIFACT_TOOL_NAME = "register_artifact_for_delivery"
_DOWNLOAD_DEFAULT_FILENAME = "download.bin"
_DOWNLOAD_CHUNK_BYTES = 64 * 1024


def _is_within_root(path: Path, root: Path) -> bool:
    """Return True when path is contained by root."""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_invocation_args(invocation: dict[str, Any]) -> dict[str, Any]:
    """Normalize invocation arguments into a dictionary."""

    arguments = invocation.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("Tool arguments must be valid JSON.") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Tool arguments must be an object with url/output_dir.")


def _resolve_filename(url: str, requested_filename: str | None) -> str:
    """Return a safe output filename using explicit value or URL path."""

    if requested_filename is not None:
        cleaned = requested_filename.strip()
        if (
            not cleaned
            or cleaned in {".", ".."}
            or cleaned != os.path.basename(cleaned)
        ):
            raise ValueError("filename must be a plain file name.")
        return cleaned

    parsed = urlparse(url)
    candidate = os.path.basename(unquote(parsed.path or "").strip())
    return candidate or _DOWNLOAD_DEFAULT_FILENAME


def _validate_url(url: str) -> str:
    """Validate URL format and return the normalized value."""

    normalized = (url or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported.")
    if not parsed.netloc:
        raise ValueError("URL must include a host.")
    return normalized


def _resolve_path_within_allowed_roots(
    raw_path: str,
    *,
    workspace_root: Path,
    allowed_roots: list[Path],
) -> Path:
    """Resolve raw path and ensure it is inside one of the allowed roots."""

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = Path(os.path.realpath(candidate)).resolve(strict=False)
    if not any(_is_within_root(resolved, root) for root in allowed_roots):
        raise ValueError("path must be inside the workspace or system temp directory.")
    return resolved


async def _download_binary(
    url: str,
    target_path: Path,
    timeout_seconds: int,
    max_bytes: int,
) -> int:
    """Download URL content into target_path with limit enforcement."""

    bytes_written = 0
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout_seconds,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(target_path, "wb") as output:
                async for chunk in response.aiter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        raise ValueError(
                            f"Downloaded file exceeds {max_bytes} byte limit."
                        )
                    output.write(chunk)

    if bytes_written <= 0:
        raise ValueError("Downloaded file is empty.")

    return bytes_written


def build_custom_tools(
    *,
    working_directory: str,
    max_download_bytes: int,
    download_timeout_seconds: int,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> list[Tool]:
    """Build and return custom SDK tools for one Copilot session."""

    workspace_root = Path(os.path.realpath(working_directory)).resolve(strict=False)
    temp_root = Path(os.path.realpath(tempfile.gettempdir())).resolve(strict=False)
    allowed_roots = [workspace_root, temp_root]

    async def _download_binary_file_handler(invocation: dict[str, Any]) -> dict[str, Any]:
        target_path: Path | None = None
        try:
            arguments = _resolve_invocation_args(invocation)
            url = _validate_url(str(arguments.get("url", "")))
            output_dir_value = str(arguments.get("output_dir", "")).strip()
            if not output_dir_value:
                raise ValueError("output_dir is required.")

            output_dir = _resolve_path_within_allowed_roots(
                output_dir_value,
                workspace_root=workspace_root,
                allowed_roots=allowed_roots,
            )
            output_dir.mkdir(parents=True, exist_ok=True)

            raw_filename = arguments.get("filename")
            requested_filename = (
                None if raw_filename is None else str(raw_filename)
            )
            filename = _resolve_filename(url, requested_filename)
            target_path = Path(os.path.realpath(output_dir / filename))
            if not _is_within_root(target_path, output_dir):
                raise ValueError("filename resolves outside output_dir.")

            bytes_written = await _download_binary(
                url=url,
                target_path=target_path,
                timeout_seconds=download_timeout_seconds,
                max_bytes=max_download_bytes,
            )
            resolved_target = str(target_path.resolve(strict=False))
            return {
                "resultType": "success",
                "textResultForLlm": resolved_target,
                "sessionLog": f"Downloaded {bytes_written} bytes to {resolved_target}.",
                "toolTelemetry": {"bytes": bytes_written},
            }
        except Exception as exc:
            if target_path is not None:
                try:
                    if target_path.exists():
                        target_path.unlink()
                except OSError:
                    pass
            return {
                "resultType": "failure",
                "textResultForLlm": str(exc),
                "error": str(exc),
                "toolTelemetry": {},
            }

    async def _register_artifact_for_delivery_handler(
        invocation: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            arguments = _resolve_invocation_args(invocation)
            path_value = str(arguments.get("path", "")).strip()
            if not path_value:
                raise ValueError("path is required.")

            artifact_path = _resolve_path_within_allowed_roots(
                path_value,
                workspace_root=workspace_root,
                allowed_roots=allowed_roots,
            )
            artifact_path_str = str(artifact_path)
            if not is_sendable_artifact(artifact_path_str, max_artifact_bytes):
                raise ValueError("path is not an eligible sendable artifact.")

            caption_value = arguments.get("caption")
            caption = None
            if caption_value is not None:
                trimmed = str(caption_value).strip()
                if trimmed:
                    caption = trimmed

            intent_payload: dict[str, Any] = {
                "delivery_intent": True,
                "artifact_path": artifact_path_str,
            }
            if caption is not None:
                intent_payload["artifact_caption"] = caption

            return {
                "resultType": "success",
                "textResultForLlm": json.dumps(intent_payload, separators=(",", ":")),
                "sessionLog": f"Registered artifact for delivery: {artifact_path_str}",
                "toolTelemetry": {},
            }
        except Exception as exc:
            return {
                "resultType": "failure",
                "textResultForLlm": str(exc),
                "error": str(exc),
                "toolTelemetry": {},
            }

    return [
        Tool(
            name=DOWNLOAD_BINARY_TOOL_NAME,
            description=(
                "Download binary content from a URL into an output directory. "
                "Use for files that cannot be fetched as text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "output_dir": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["url", "output_dir"],
            },
            handler=_download_binary_file_handler,
        ),
        Tool(
            name=REGISTER_ARTIFACT_TOOL_NAME,
            description=(
                "Register an already generated local artifact file for Telegram delivery."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "caption": {"type": "string"},
                },
                "required": ["path"],
            },
            handler=_register_artifact_for_delivery_handler,
        ),
    ]
