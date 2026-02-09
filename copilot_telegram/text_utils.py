"""Text formatting helpers for Telegram messaging limits."""


def split_for_telegram(text: str, chunk_size: int = 4096) -> list[str]:
    """Split a message into Telegram-safe chunks, preferring newline breaks."""

    if len(text) <= chunk_size:
        return [text]

    parts: list[str] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        if end < text_length:
            newline_pos = text.rfind("\n", start, end)
            if newline_pos > start:
                end = newline_pos + 1

        chunk = text[start:end]
        if not chunk:
            # Defensive fallback to avoid stalling if boundary math ever regresses.
            end = min(start + chunk_size, text_length)
            chunk = text[start:end]
        parts.append(chunk)
        start = end
    return parts
