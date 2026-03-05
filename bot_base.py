import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ChatMemberHandler, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from telegram.helpers import escape_markdown

from config import BotConfig, ChannelConfig, Settings, Template, load_templates, select_template
from template_editor import load_group_templates, save_group_templates, register_template_handlers

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
        self._restore_saved_groups()

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

    def _restore_saved_groups(self) -> None:
        """Restore groups from saved data files on startup."""
        import re  # local import to keep module-level clean
        data_dir = Path(self.config.settings.data_dir)
        if not data_dir.exists():
            return
        for f in data_dir.glob("group_*.json"):
            match = re.match(r"group_(-?\d+)\.json", f.name)
            if not match:
                continue
            gid = int(match.group(1))
            if gid in self._channel_settings:
                continue  # already configured
            templates = load_group_templates(str(data_dir), gid)
            if templates:
                settings = self.config.settings
                meta = self._load_groups_meta()
                saved_delay = meta.get(str(gid), {}).get(
                    "reply_delay_seconds", settings.default_reply_delay_seconds
                )
                ch = ChannelConfig(
                    channel_id=0,
                    discussion_group_id=gid,
                    template_file=settings.default_template_file,
                    reply_delay_seconds=saved_delay,
                )
                self._templates[gid] = templates
                self._channel_settings[gid] = ch
                logger.info("[%s] Restored group %d from saved data (%d templates, delay %ds)", self.name, gid, len(templates), saved_delay)

    # ---- Group metadata persistence ----

    def _meta_path(self) -> Path:
        return Path(self.config.settings.data_dir) / "groups_meta.json"

    def _load_groups_meta(self) -> dict:
        """Load groups metadata from disk."""
        path = self._meta_path()
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_groups_meta(self, meta: dict) -> None:
        """Save groups metadata to disk."""
        path = self._meta_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def _record_group_added(self, chat, from_user, status: str) -> None:
        """Record who added the bot to a group."""
        meta = self._load_groups_meta()
        meta[str(chat.id)] = {
            "group_title": chat.title or "",
            "group_id": chat.id,
            "added_by_name": from_user.full_name if from_user else "未知",
            "added_by_id": from_user.id if from_user else 0,
            "bot_status": status,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_groups_meta(meta)

    def _remove_group_meta(self, group_id: int) -> None:
        """Remove a group from metadata."""
        meta = self._load_groups_meta()
        meta.pop(str(group_id), None)
        self._save_groups_meta(meta)

    def select_template(self, discussion_group_id: int) -> Template | None:
        """Select a comment template for the given discussion group. Override for custom logic."""
        templates = self._templates.get(discussion_group_id, [])
        if not templates:
            return None
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

        # Try loading saved group templates first, then fall back to default
        templates = load_group_templates(settings.data_dir, discussion_group_id)
        if templates:
            logger.info("[%s] Loaded saved templates for group %d from data dir", self.name, discussion_group_id)
        else:
            try:
                templates = load_templates(ch.template_file)
            except (FileNotFoundError, ValueError) as e:
                logger.error("[%s] Auto-register failed for group %d: %s", self.name, discussion_group_id, e)
                return

        self._templates[discussion_group_id] = templates
        self._channel_settings[discussion_group_id] = ch

        # Persist so the group survives bot restarts
        save_group_templates(settings.data_dir, discussion_group_id, templates)

        logger.info(
            "[%s] Auto-registered group %d (channel %d, template: %s, delay: %ds)",
            self.name, discussion_group_id, channel_id, ch.template_file, ch.reply_delay_seconds,
        )

    async def _handle_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle bot being added to or promoted in a group — auto-register and notify owner."""
        member_update = update.my_chat_member
        if not member_update:
            return

        chat = member_update.chat
        # Only care about groups/supergroups
        if chat.type not in ("group", "supergroup"):
            return

        from_user = member_update.from_user
        new_status = member_update.new_chat_member.status
        old_status = member_update.old_chat_member.status
        owner_id = self.config.settings.owner_id

        if new_status in ("administrator", "member") and old_status in ("left", "kicked", "restricted"):
            # Bot was added to / promoted in a group
            if chat.id not in self._channel_settings:
                self._auto_register_group(chat.id, channel_id=0)
                logger.info("[%s] Bot joined/promoted in group %d, auto-registered", self.name, chat.id)

            self._record_group_added(chat, from_user, new_status)

            if owner_id:
                user_name = from_user.full_name if from_user else "未知用户"
                user_id = from_user.id if from_user else 0
                status_text = "管理员" if new_status == "administrator" else "成员"
                msg = (
                    f"\U0001f514 Bot 被添加到新群组\n\n"
                    f"群组：{chat.title} ({chat.id})\n"
                    f"操作人：{user_name} ({user_id})\n"
                    f"Bot 身份：{status_text}"
                )
                try:
                    await context.bot.send_message(chat_id=owner_id, text=msg)
                except Exception as e:
                    logger.warning("[%s] Failed to notify owner: %s", self.name, e)

        elif new_status in ("left", "kicked"):
            # Bot was removed from the group — clean up
            self._channel_settings.pop(chat.id, None)
            self._templates.pop(chat.id, None)
            self._remove_group_meta(chat.id)
            logger.info("[%s] Bot removed from group %d, unregistered", self.name, chat.id)

            if owner_id:
                user_name = from_user.full_name if from_user else "未知用户"
                msg = f"\u26a0\ufe0f Bot 已被移出群组：{chat.title} ({chat.id})\n操作人：{user_name}"
                try:
                    await context.bot.send_message(chat_id=owner_id, text=msg)
                except Exception as e:
                    logger.warning("[%s] Failed to notify owner: %s", self.name, e)

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

        template = self.select_template(chat_id)
        if not template:
            logger.debug("[%s] No active templates for group %d, skipping", self.name, chat_id)
            return

        delay = channel_config.reply_delay_seconds
        if delay > 0:
            logger.debug("[%s] Waiting %ds before replying in group %d", self.name, delay, chat_id)
            await asyncio.sleep(delay)

        try:
            escaped_text = escape_markdown(template.text, version=2)

            # Build inline keyboard from template buttons (rows of buttons)
            reply_markup = None
            if template.buttons:
                kb_rows = []
                for row in template.buttons:
                    kb_row = []
                    for btn in row:
                        kwargs = {}
                        if btn.get("style"):
                            kwargs["api_kwargs"] = {"style": btn["style"]}
                        kb_row.append(InlineKeyboardButton(
                            text=btn["text"], url=btn["url"], **kwargs,
                        ))
                    kb_rows.append(kb_row)
                reply_markup = InlineKeyboardMarkup(kb_rows)

            await message.reply_text(
                escaped_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup,
            )
            logger.info(
                "[%s] Replied in group %d (message %d): %s",
                self.name, chat_id, message.message_id, template.text[:50],
            )
        except Exception as e:
            logger.error("[%s] Failed to reply in group %d: %s", self.name, chat_id, e)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "\U0001f44b 你好！我是频道评论 Bot。\n\n"
            "将我添加到群组并设为管理员后，我会自动回复频道转发的消息。\n\n"
            "可用命令：\n"
            "/templates - 编辑评论模板\n"
            "/groups - 查看管理的群组（仅 Owner）\n"
            "/contact - 联系客服\n"
            "/help - 查看帮助"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        await update.message.reply_text(
            "\U0001f4d6 使用帮助\n\n"
            "1. 将 Bot 添加到讨论群组并设为管理员\n"
            "2. 私聊 Bot 发送 /templates 编辑评论模板\n"
            "3. 频道发布消息后，Bot 会自动在讨论群回复\n\n"
            "命令列表：\n"
            "/start - 开始使用\n"
            "/templates - 编辑评论模板\n"
            "/groups - 查看管理的群组（仅 Owner）\n"
            "/contact - 联系客服\n"
            "/help - 查看帮助\n"
            "/cancel - 取消当前操作"
        )

    async def _cmd_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /groups command — owner-only, show all managed groups."""
        owner_id = self.config.settings.owner_id
        if not update.effective_user or update.effective_user.id != owner_id:
            await update.message.reply_text("\u26a0\ufe0f 仅 Bot 管理员可使用此命令。")
            return

        meta = self._load_groups_meta()
        if not meta:
            await update.message.reply_text("当前没有群组记录。")
            return

        lines = [f"\U0001f4cb Bot 管理的群组（共 {len(meta)} 个）\n"]
        for i, (gid, info) in enumerate(meta.items(), 1):
            title = info.get("group_title") or "未知群组"
            added_by = info.get("added_by_name", "未知")
            added_by_id = info.get("added_by_id", 0)
            status = "管理员" if info.get("bot_status") == "administrator" else "成员"
            added_at = info.get("added_at", "")[:10]  # date only
            tpl_count = len(self._templates.get(int(gid), []))
            lines.append(
                f"{i}. {title}\n"
                f"   ID: {gid}\n"
                f"   添加人: {added_by} ({added_by_id})\n"
                f"   Bot 身份: {status}\n"
                f"   模板数: {tpl_count}\n"
                f"   添加时间: {added_at}"
            )

        await update.message.reply_text("\n".join(lines))

    # ---- Contact / customer-service mode ----

    async def _cmd_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /contact — enter customer-service mode (private chat only)."""
        if update.effective_chat.type != "private":
            await update.message.reply_text("\u26a0\ufe0f 请在私聊中使用 /contact 命令。")
            return
        context.user_data["contact_mode"] = True
        await update.message.reply_text(
            "\U0001f4e8 已进入客服模式，请发送您的问题。\n完成后发送 /cancel 退出。"
        )

    async def _handle_contact_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward user messages to the owner while in contact mode."""
        if not context.user_data.get("contact_mode"):
            return
        owner_id = self.config.settings.owner_id
        if not owner_id:
            await update.message.reply_text("\u26a0\ufe0f 客服功能暂不可用。")
            return

        user = update.effective_user
        try:
            forwarded = await update.message.forward(chat_id=owner_id)
            # Send an annotation so the owner knows who sent it
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"\U0001f464 来自 {user.full_name}（ID: {user.id}）",
            )
            # Map the forwarded message id to the user so owner can reply
            contact_map = context.bot_data.setdefault("contact_map", {})
            contact_map[forwarded.message_id] = user.id
            await update.message.reply_text("\u2705 已收到，请等待客服回复。")
        except Exception as e:
            logger.error("[%s] Failed to forward contact message: %s", self.name, e)
            await update.message.reply_text("\u26a0\ufe0f 发送失败，请稍后再试。")

    async def _handle_owner_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward the owner's reply back to the corresponding user."""
        reply_msg = update.message.reply_to_message
        if not reply_msg:
            return
        contact_map: dict = context.bot_data.get("contact_map", {})
        user_id = contact_map.get(reply_msg.message_id)
        if user_id is None:
            return
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"\U0001f4ac 客服回复：\n{update.message.text}",
            )
        except Exception as e:
            logger.error("[%s] Failed to send owner reply to user %d: %s", self.name, user_id, e)
            await update.message.reply_text(f"\u26a0\ufe0f 回复发送失败: {e}")

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cancel — exit contact mode (standalone, outside ConversationHandler)."""
        if context.user_data.pop("contact_mode", None):
            await update.message.reply_text("\u2705 已退出客服模式。")
        else:
            await update.message.reply_text("当前没有进行中的操作。")

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

        # Auto-register groups when bot is added/promoted
        app.add_handler(ChatMemberHandler(self._handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

        # Command handlers
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("groups", self._cmd_groups))

        # Register template editor handlers
        register_template_handlers(app, self)

        # Contact / customer-service handlers
        app.add_handler(CommandHandler("contact", self._cmd_contact))
        owner_id = self.config.settings.owner_id
        if owner_id:
            owner_reply_filter = (
                filters.Chat(owner_id)
                & filters.REPLY
                & filters.ChatType.PRIVATE
                & ~filters.COMMAND
            )
            app.add_handler(MessageHandler(owner_reply_filter, self._handle_owner_reply))
        contact_filter = (
            filters.ChatType.PRIVATE
            & ~filters.COMMAND
            & (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL)
        )
        app.add_handler(MessageHandler(contact_filter, self._handle_contact_message))

        # Standalone /cancel (outside ConversationHandler, e.g. for contact mode)
        app.add_handler(CommandHandler("cancel", self._cmd_cancel))

        logger.info("[%s] Application built with %d channel(s)", self.name, len(self._channel_settings))
        return app

    async def start(self) -> None:
        """Initialize and start polling."""
        self.application = self.build_application()
        await self.application.initialize()
        await self.application.start()

        # Register bot menu commands
        await self.application.bot.set_my_commands([
            BotCommand("start", "开始使用"),
            BotCommand("help", "查看帮助"),
            BotCommand("templates", "编辑评论模板"),
            BotCommand("groups", "查看管理的群组"),
            BotCommand("contact", "联系客服"),
        ])

        await self.application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "my_chat_member"],
        )
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
