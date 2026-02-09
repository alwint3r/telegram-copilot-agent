"""Helpers for SDK user-input fallback behavior."""

import os

from copilot.types import UserInputResponse


def default_user_input_answer(
    choices: list[str], allow_freeform: bool
) -> UserInputResponse:
    """Pick a safe default answer for unattended Copilot input requests."""

    if choices:
        lowered = {choice.lower(): choice for choice in choices}
        for key in ("no", "cancel", "deny", "abort", "skip"):
            if key in lowered:
                return {"answer": lowered[key], "wasFreeform": False}
        return {"answer": choices[0], "wasFreeform": False}

    default_text = (
        os.getenv("COPILOT_USER_INPUT_DEFAULT_ANSWER")
        or "Proceed using your best judgment and continue."
    )
    return {"answer": default_text, "wasFreeform": allow_freeform}
