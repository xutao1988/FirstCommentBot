import asyncio
import logging

from bot_base import ChannelReviewBot
from config import AppConfig

logger = logging.getLogger(__name__)

# Registry of bot class names to classes
BOT_CLASS_REGISTRY: dict[str, type[ChannelReviewBot]] = {
    "ChannelReviewBot": ChannelReviewBot,
}


def register_bot_class(name: str, cls: type[ChannelReviewBot]) -> None:
    """Register a custom bot class so it can be referenced in config.json."""
    BOT_CLASS_REGISTRY[name] = cls


class BotManager:
    """Manages multiple bot instances with concurrent startup and graceful shutdown."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.bots: list[ChannelReviewBot] = []
        self._stop_event = asyncio.Event()

    def _create_bots(self) -> None:
        """Instantiate bot objects from config."""
        for bot_config in self.config.bots:
            cls = BOT_CLASS_REGISTRY.get(bot_config.bot_class, ChannelReviewBot)
            bot = cls(bot_config)
            self.bots.append(bot)
            logger.info("Created bot: %s (class: %s)", bot.name, cls.__name__)

    async def start_all(self) -> None:
        """Start all bots concurrently."""
        self._create_bots()
        if not self.bots:
            logger.warning("No bots configured. Exiting.")
            return

        start_tasks = [bot.start() for bot in self.bots]
        results = await asyncio.gather(*start_tasks, return_exceptions=True)

        for bot, result in zip(self.bots, results):
            if isinstance(result, Exception):
                logger.error("Failed to start bot %s: %s", bot.name, result)

        started = sum(1 for r in results if not isinstance(r, Exception))
        logger.info("Started %d/%d bots", started, len(self.bots))

    async def stop_all(self) -> None:
        """Stop all bots gracefully."""
        logger.info("Stopping all bots...")
        stop_tasks = [bot.stop() for bot in self.bots]
        results = await asyncio.gather(*stop_tasks, return_exceptions=True)

        for bot, result in zip(self.bots, results):
            if isinstance(result, Exception):
                logger.error("Error stopping bot %s: %s", bot.name, result)

        logger.info("All bots stopped")

    async def run(self) -> None:
        """Start all bots and wait until stopped."""
        await self.start_all()
        try:
            await self._stop_event.wait()
        finally:
            await self.stop_all()

    def request_stop(self) -> None:
        """Signal the manager to stop."""
        self._stop_event.set()
