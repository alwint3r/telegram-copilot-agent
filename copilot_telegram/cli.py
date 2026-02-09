"""CLI wiring for the Telegram bot runtime."""

import argparse
import logging
import os

from copilot import CopilotClient
from copilot.types import CopilotClientOptions
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .config import load_startup_config
from .dispatcher import BackgroundDispatcher
from .handlers import (
    install_shutdown_signal_handlers,
    message_handler,
    on_shutdown,
    on_startup,
    reset_command,
    restore_signal_handlers,
    start_command,
)
from .session_manager import CopilotSessionManager


def configure_logging() -> None:
    """Configure application-wide logging for the bot process."""

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create command-line parser for runtime startup options."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--working-directory",
        "-w",
        help="Working directory.",
        default=os.getcwd(),
        required=True,
    )
    parser.add_argument("--skills-directory", "-s", help="Skills directory")
    return parser


def main() -> None:
    """Build and run the Telegram polling application."""

    load_dotenv()
    configure_logging()

    arg_parser = build_arg_parser()
    args = arg_parser.parse_args()
    startup_config = load_startup_config()

    client_options: CopilotClientOptions = {"log_level": startup_config.log_level}
    client = CopilotClient(client_options)
    manager = CopilotSessionManager(
        client=client,
        model=startup_config.model,
        timeout_seconds=startup_config.timeout_seconds,
        reasoning_effort=startup_config.reasoning_effort,
        working_directory=args.working_directory,
    )

    app = (
        Application.builder()
        .token(startup_config.api_key)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.bot_data["copilot_client"] = client
    app.bot_data["copilot_manager"] = manager
    dispatcher = BackgroundDispatcher(app, manager)
    manager.set_user_input_requester(dispatcher.request_user_input)
    app.bot_data["background_dispatcher"] = dispatcher

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))

    previous_signal_handlers = install_shutdown_signal_handlers(app, dispatcher)
    try:
        app.run_polling(stop_signals=None)
    finally:
        restore_signal_handlers(previous_signal_handlers)
