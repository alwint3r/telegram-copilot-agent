"""Telegram command/message handlers and lifecycle hooks."""

from contextlib import suppress
import logging
import signal
from typing import Any

from copilot import CopilotClient
from telegram import Update
from telegram.ext import Application, ContextTypes

from .dispatcher import BackgroundDispatcher
from .session_manager import CopilotSessionManager

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/start` and explain how to use the bot."""

    _ = context
    if update.message:
        await update.message.reply_text("Send me a message and I will ask Copilot.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/reset` by clearing the active Copilot session for this chat."""

    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    manager: CopilotSessionManager | None = context.application.bot_data.get(
        "copilot_manager"
    )
    if manager is None:
        await message.reply_text(
            "Bot is still initializing. Please try again in a moment."
        )
        return

    await message.reply_text(
        "Reset requested. Waiting for any active Copilot request to finish...",
        allow_sending_without_reply=True,
    )

    reset_applied = await manager.reset_session(chat.id)
    if reset_applied:
        await message.reply_text(
            "Conversation history reset. Your next message starts a fresh Copilot session.",
            allow_sending_without_reply=True,
        )
        return

    await message.reply_text(
        "No active Copilot session was found. Your next message will still start fresh.",
        allow_sending_without_reply=True,
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inbound chat messages and enqueue them for background execution."""

    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or message.text is None:
        return

    dispatcher: BackgroundDispatcher | None = context.application.bot_data.get(
        "background_dispatcher"
    )
    if dispatcher is None:
        await message.reply_text(
            "Bot is still initializing. Please try again in a moment."
        )
        return

    consumed, reply_text = await dispatcher.consume_user_input_reply(
        chat_id=chat.id, text=message.text
    )
    if consumed:
        if reply_text:
            await message.reply_text(reply_text, allow_sending_without_reply=True)
        return

    pending = await dispatcher.enqueue(
        chat_id=chat.id,
        text=message.text,
        reply_to_message_id=message.message_id,
    )
    if pending is None:
        await message.reply_text(
            "Shutdown in progress. I am no longer accepting new requests.",
            allow_sending_without_reply=True,
        )
        return

    if pending > 1:
        await message.reply_text(
            f"Request queued ({pending} pending). I will process it in the background."
        )
    else:
        await message.reply_text(
            "Request received. I will process it in the background."
        )


async def on_startup(application: Application) -> None:
    """Start the Copilot SDK client once the Telegram app is ready."""

    client: CopilotClient = application.bot_data["copilot_client"]
    await client.start()


async def on_shutdown(application: Application) -> None:
    """Shutdown workers, sessions, and the Copilot SDK client."""

    dispatcher: BackgroundDispatcher = application.bot_data["background_dispatcher"]
    manager: CopilotSessionManager = application.bot_data["copilot_manager"]
    client: CopilotClient = application.bot_data["copilot_client"]
    await dispatcher.shutdown()
    await manager.shutdown()
    await client.stop()


def install_shutdown_signal_handlers(
    app: Application,
    dispatcher: BackgroundDispatcher,
) -> dict[int, Any]:
    """Install SIGINT/SIGTERM handlers that trigger graceful then forced shutdown."""

    previous_handlers: dict[int, Any] = {}

    def _handle_signal(signum: int, _frame: Any) -> None:
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = str(signum)

        first_signal = dispatcher.request_shutdown(force=False)
        if first_signal:
            logger.info(
                "Received %s. Starting graceful shutdown (drain timeout=%ss).",
                signal_name,
                dispatcher.shutdown_drain_timeout_seconds,
            )
        else:
            dispatcher.request_shutdown(force=True)
            logger.warning(
                "Received %s again. Escalating to forced shutdown.",
                signal_name,
            )

        with suppress(Exception):
            app.stop_running()

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)

    for signum in signals:
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_signal)

    return previous_handlers


def restore_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    """Restore previously registered OS signal handlers."""

    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)
