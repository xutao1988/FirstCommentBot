import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from config import BotConfig, ChannelConfig, Settings, Template, load_templates, select_template

logger = logging.getLogger(__name__)


class ChannelReviewBot:
    """Base class for channel auto-comment bots.

    Detects automatic forwards from channels to discussion groups
    and replies with a randomly selected comment template.

    Subclasses can override:
        - select_template(): customize template selection logic
        - _handle_auto_forward(): customize the reply behavior
        - build_application(): add extra handlers or configuration
    """

    def __init__(self, bot_config: BotConfig):
        self.config = bot_config
        self.name = bot_config.name
        self._templates: dict[int, list[Template]] = {}  # discussion_group_id -> templates
        self._channel_settings: dict[int, ChannelConfig] = {}  # discussion_group_id -> config
        self.application: Application | None = None

        self._load_all_templates()

    def _load_all_templates(self) -> None:
        """Pre-load templates for all configured channels."""
        for ch in self.config.channels:
            try:
                templates = load_templates(ch.template_file)
                self._templates[ch.discussion_group_id] = templates
                self._channel_settings[ch.discussion_group_id] = ch
                logger.info(
                    "[%s] Loaded %d templates for discussion group %d (file: %s)",
                    self.name, len(templates), ch.discussion_group_id, ch.template_file,
                )
            except (FileNotFoundError, ValueError) as e:
                logger.error("[%s] Failed to load templates for group %d: %s", self.name, ch.discussion_group_id, e)

    def select_template(self, discussion_group_id: int) -> str:
        """Select a comment template for the given discussion group. Override for custom logic."""
        templates = self._templates.get(discussion_group_id, [])
        if not templates:
            raise ValueError(f"No templates available for discussion group {discussion_group_id}")
        return select_template(templates)

    def _auto_register_group(self, discussion_group_id: int, channel_id: int) -> None:
        """Auto-register a newly discovered discussion group with default settings."""
        settings: Settings = self.config.settings
        ch = ChannelConfig(
            channel_id=channel_id,
            discussion_group_id=discussion_group_id,
            template_file=settings.default_template_file,
            reply_delay_seconds=settings.default_reply_delay_seconds,
        )
        try:
            templates = load_templates(ch.template_file)
        except (FileNotFoundError, ValueError) as e:
            logger.error("[%s] Auto-register failed for group %d: %s", self.name, discussion_group_id, e)
            return
        self._templates[discussion_group_id] = templates
        self._channel_settings[discussion_group_id] = ch
        logger.info(
            "[%s] Auto-registered group %d (channel %d, template: %s, delay: %ds)",
            self.name, discussion_group_id, channel_id, ch.template_file, ch.reply_delay_seconds,
        )

    async def _handle_auto_forward(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle an automatic forward from a channel to its discussion group.

        Override this method to customize reply behavior.
        """
        message = update.effective_message
        if not message:
            return

        chat_id = message.chat_id

        if chat_id not in self._channel_settings:
            # Auto-discovery: register this group on first forwarded message
            sender_chat = message.sender_chat
            channel_id = sender_chat.id if sender_chat else 0
            self._auto_register_group(chat_id, channel_id)
            if chat_id not in self._channel_settings:
                return  # registration failed

        channel_config = self._channel_settings[chat_id]

        try:
            text = self.select_template(chat_id)
        except ValueError as e:
            logger.error("[%s] Template selection failed: %s", self.name, e)
            return

        delay = channel_config.reply_delay_seconds
        if delay > 0:
            logger.debug("[%s] Waiting %ds before replying in group %d", self.name, delay, chat_id)
            await asyncio.sleep(delay)

        try:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(
                "[%s] Replied in group %d (message %d): %s",
                self.name, chat_id, message.message_id, text[:50],
            )
        except Exception as e:
            logger.error("[%s] Failed to reply in group %d: %s", self.name, chat_id, e)

    def _build_auto_forward_filter(self) -> filters.BaseFilter:
        """Build a combined filter for all configured discussion groups.

        With pre-configured channels: restrict to known group IDs.
        Without pre-configured channels: match any automatic forward (auto-discovery mode).
        """
        group_ids = list(self._channel_settings.keys())
        if group_ids:
            chat_filter = filters.Chat(chat_id=group_ids)
            return filters.IS_AUTOMATIC_FORWARD & chat_filter
        # Auto-discovery mode: accept automatic forwards from any group
        return filters.IS_AUTOMATIC_FORWARD

    def build_application(self) -> Application:
        """Build and return the Application instance. Override to add extra handlers."""
        app = Application.builder().token(self.config.token).build()

        combined_filter = self._build_auto_forward_filter()
        app.add_handler(MessageHandler(combined_filter, self._handle_auto_forward))

        logger.info("[%s] Application built with %d channel(s)", self.name, len(self._channel_settings))
        return app

    async def start(self) -> None:
        """Initialize and start polling."""
        self.application = self.build_application()
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)
        logger.info("[%s] Started polling", self.name)

    async def stop(self) -> None:
        """Stop polling and shut down gracefully."""
        if not self.application:
            return
        logger.info("[%s] Stopping...", self.name)
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
        logger.info("[%s] Stopped", self.name)
