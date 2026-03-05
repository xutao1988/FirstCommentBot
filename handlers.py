import logging
import random

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot_base import ChannelReviewBot
from bot_manager import register_bot_class
from config import BotConfig

logger = logging.getLogger(__name__)


class CustomReviewBot(ChannelReviewBot):
    """Example subclass demonstrating how to extend the base bot.

    - Overrides select_template() to avoid repeating the last comment.
    - Adds a /status command handler.
    """

    def __init__(self, bot_config: BotConfig):
        super().__init__(bot_config)
        self._last_used: dict[int, str] = {}  # discussion_group_id -> last text

    def select_template(self, discussion_group_id: int) -> str:
        """Select a template, avoiding immediate repeats."""
        templates = self._templates.get(discussion_group_id, [])
        if not templates:
            raise ValueError(f"No templates for group {discussion_group_id}")

        last = self._last_used.get(discussion_group_id)
        candidates = [t for t in templates if t.text != last] or templates

        texts = [t.text for t in candidates]
        weights = [t.weight for t in candidates]
        chosen = random.choices(texts, weights=weights, k=1)[0]

        self._last_used[discussion_group_id] = chosen
        return chosen

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command - report which channels are monitored."""
        lines = [f"Bot: {self.name}", f"Monitoring {len(self._channel_settings)} channel(s):"]
        for gid, ch in self._channel_settings.items():
            lines.append(f"  - Group {gid} (delay: {ch.reply_delay_seconds}s)")
        await update.message.reply_text("\n".join(lines))

    def build_application(self) -> Application:
        """Extend the base application with extra command handlers."""
        app = super().build_application()
        app.add_handler(CommandHandler("status", self._cmd_status))
        logger.info("[%s] Added /status command handler", self.name)
        return app


def register_custom_bots() -> None:
    """Register all custom bot classes so they can be used in config.json."""
    register_bot_class("CustomReviewBot", CustomReviewBot)
