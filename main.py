import asyncio
import logging
import signal
import sys

from dotenv import load_dotenv

from bot_manager import BotManager
from config import load_config


def setup_logging(log_file: str) -> None:
    """Configure logging to both console and file."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    # Reduce noise from httpx / httpcore
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def main() -> None:
    load_dotenv()

    app_config = load_config()
    setup_logging(app_config.settings.log_file)

    logger = logging.getLogger(__name__)
    logger.info("Configuration loaded: %d bot(s)", len(app_config.bots))

    # Register custom bot classes before creating manager
    # Import here to avoid circular imports and to register classes
    from handlers import register_custom_bots
    register_custom_bots()

    manager = BotManager(app_config)

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        manager.request_stop()

    # Register signal handlers (Unix-style; on Windows use alternative)
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
    else:
        # On Windows, handle KeyboardInterrupt via the except block below
        pass

    try:
        await manager.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
        manager.request_stop()
        await manager.stop_all()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
