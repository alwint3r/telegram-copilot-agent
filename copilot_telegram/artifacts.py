"""Artifact discovery and filtering helpers."""

import mimetypes
import os
from pathlib import Path
from typing import Any

from .constants import DIRECT_SEND_EXTENSIONS, TEXTUAL_ARTIFACT_EXTENSIONS


def normalize_workspace_path(value: str, workspace_root: str) -> str | None:
    """Normalize a path and keep it inside the workspace boundary."""

    if not value:
        return None

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(workspace_root) / candidate
    resolved = candidate.resolve(strict=False)
    root_resolved = Path(workspace_root).resolve(strict=False)

    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None

    return str(resolved)


def extract_existing_paths_from_obj(
    payload: Any,
    workspace_root: str,
    depth: int = 0,
) -> set[str]:
    """Recursively collect existing file paths from nested payload objects."""

    if payload is None or depth > 4:
        return set()

    paths: set[str] = set()
    if isinstance(payload, dict):
        for value in payload.values():
            paths.update(
                extract_existing_paths_from_obj(value, workspace_root, depth + 1)
            )
        return paths

    if isinstance(payload, (list, tuple)):
        for value in payload:
            paths.update(
                extract_existing_paths_from_obj(value, workspace_root, depth + 1)
            )
        return paths

    if isinstance(payload, str):
        maybe_path = normalize_workspace_path(payload.strip(), workspace_root)
        if maybe_path and os.path.isfile(maybe_path):
            paths.add(maybe_path)
    return paths


def is_sendable_artifact(path: str, max_artifact_bytes: int) -> bool:
    """Return True when a file should be sent back to Telegram as an artifact."""

    if not os.path.isfile(path):
        return False
    extension = Path(path).suffix.lower()
    if extension in TEXTUAL_ARTIFACT_EXTENSIONS:
        return False
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return False
    if size_bytes <= 0 or size_bytes > max_artifact_bytes:
        return False
    if extension in DIRECT_SEND_EXTENSIONS:
        return True
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        return False
    if mime_type.startswith("text/"):
        return False
    return mime_type.startswith(("image/", "audio/", "video/")) or mime_type in {
        "application/pdf",
        "application/zip",
    }
