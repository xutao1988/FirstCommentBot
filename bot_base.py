import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler,
    MessageHandler, filters, ContextTypes,
)


from config import BotConfig, ChannelConfig, Settings, Template, escape_markdown_v2, load_templates, select_template
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
        self._post_counter: dict[int, int] = {}  # discussion_group_id -> post count
        self._seen_media_groups: dict[int, set[str]] = {}  # gid -> set of media_group_id
        self.application: Application | None = None
        self._manager = None  # set by BotManager after creation

        self._daily_stats: dict[int, dict] = {}  # gid -> {"posts_seen": N, "replies_sent": N}
        self._load_daily_stats()

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
                group_meta = meta.get(str(gid), {})
                saved_delay = group_meta.get(
                    "reply_delay_seconds", settings.default_reply_delay_seconds
                )
                saved_interval = group_meta.get("reply_interval", 1)
                ch = ChannelConfig(
                    channel_id=0,
                    discussion_group_id=gid,
                    template_file=settings.default_template_file,
                    reply_delay_seconds=saved_delay,
                    reply_interval=saved_interval,
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

    # ---- Clone metadata persistence ----

    def _clones_meta_path(self) -> Path:
        return Path(self.config.settings.data_dir) / "clones_meta.json"

    def _load_clones_meta(self) -> dict:
        path = self._clones_meta_path()
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_clones_meta(self, meta: dict) -> None:
        path = self._clones_meta_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # ---- Daily stats persistence ----

    def _stats_path(self) -> Path:
        return Path(self.config.settings.data_dir) / "daily_stats.json"

    def _load_daily_stats(self) -> None:
        path = self._stats_path()
        if path.exists():
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self._daily_stats = {int(k): v for k, v in raw.items()}
        else:
            self._daily_stats = {}

    def _save_daily_stats(self) -> None:
        path = self._stats_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in self._daily_stats.items()}, f, ensure_ascii=False, indent=2)

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

        # Deduplicate album (media group) — only reply to the first message
        if message.media_group_id:
            seen = self._seen_media_groups.setdefault(chat_id, set())
            if message.media_group_id in seen:
                logger.debug("[%s] Skipping duplicate media_group %s in group %d", self.name, message.media_group_id, chat_id)
                return
            seen.add(message.media_group_id)
            # Prevent unbounded growth: keep only the last 200 entries per group
            if len(seen) > 200:
                excess = len(seen) - 200
                for _ in range(excess):
                    seen.pop()

        # Track posts seen
        stats = self._daily_stats.setdefault(chat_id, {"posts_seen": 0, "replies_sent": 0})
        stats["posts_seen"] += 1
        self._save_daily_stats()

        if chat_id not in self._channel_settings:
            # Auto-discovery: register this group on first forwarded message
            sender_chat = message.sender_chat
            channel_id = sender_chat.id if sender_chat else 0
            self._auto_register_group(chat_id, channel_id)
            if chat_id not in self._channel_settings:
                return  # registration failed

        channel_config = self._channel_settings[chat_id]

        # Interval check: skip if not at the Nth post
        interval = channel_config.reply_interval
        if interval > 1:
            counter = self._post_counter.get(chat_id, 0) + 1
            self._post_counter[chat_id] = counter
            if counter % interval != 0:
                logger.debug("[%s] Interval skip for group %d (%d/%d)", self.name, chat_id, counter, interval)
                return

        template = self.select_template(chat_id)
        if not template:
            logger.debug("[%s] No active templates for group %d, skipping", self.name, chat_id)
            return

        delay = channel_config.reply_delay_seconds
        if delay > 0:
            logger.debug("[%s] Waiting %ds before replying in group %d", self.name, delay, chat_id)
            await asyncio.sleep(delay)

        try:
            escaped_text = escape_markdown_v2(template.text)

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

            if template.media_file_id and template.media_type:
                send = {
                    "photo": message.reply_photo,
                    "animation": message.reply_animation,
                    "video": message.reply_video,
                }.get(template.media_type)
                if send:
                    await send(
                        template.media_file_id,
                        caption=escaped_text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=reply_markup,
                    )
                else:
                    await message.reply_text(
                        escaped_text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=reply_markup,
                    )
            else:
                await message.reply_text(
                    escaped_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup,
                )
            # Track replies sent
            stats = self._daily_stats.setdefault(chat_id, {"posts_seen": 0, "replies_sent": 0})
            stats["replies_sent"] += 1
            self._save_daily_stats()

            logger.info(
                "[%s] Replied in group %d (message %d): %s",
                self.name, chat_id, message.message_id, template.text[:50],
            )
        except Exception as e:
            logger.error("[%s] Failed to reply in group %d: %s", self.name, chat_id, e)

    # ---- Daily report scheduling ----

    def _schedule_daily_report(self) -> None:
        """Register daily report job if stats_channel_id is configured."""
        stats_channel_id = self.config.settings.stats_channel_id
        if not stats_channel_id:
            return
        if not self.application.job_queue:
            logger.error("[%s] job_queue unavailable — install python-telegram-bot[job-queue]", self.name)
            return
        tz = ZoneInfo("Asia/Shanghai")
        report_time = time(hour=8, minute=34, tzinfo=tz)
        self.application.job_queue.run_daily(
            self._send_daily_report,
            time=report_time,
            name=f"{self.name}_daily_report",
        )
        logger.info("[%s] Daily report job scheduled at 08:34 Asia/Shanghai -> channel %d", self.name, stats_channel_id)

    async def _send_daily_report(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Job callback: build and send the daily stats report, then reset counters."""
        stats_channel_id = self.config.settings.stats_channel_id
        if not stats_channel_id:
            return

        tz = ZoneInfo("Asia/Shanghai")
        # The job runs at 08:34, report covers the previous day
        report_date = datetime.now(tz).date() - timedelta(days=1)

        meta = self._load_groups_meta()
        clones = self._load_clones_meta()
        group_count = len(self._channel_settings)
        clone_count = len(clones)

        lines = [
            f"\U0001f4ca 每日统计 — {report_date}",
            "",
            f"\U0001f465 管理群组：{group_count} 个",
            f"\U0001f4e6 克隆 Bot：{clone_count} 个",
            "",
            "\U0001f4c8 各群组数据：",
        ]

        total_posts = 0
        total_replies = 0
        idx = 0

        for gid, ch_cfg in self._channel_settings.items():
            idx += 1
            group_meta = meta.get(str(gid), {})
            title = group_meta.get("group_title") or "未知群组"
            stats = self._daily_stats.get(gid, {"posts_seen": 0, "replies_sent": 0})
            posts = stats.get("posts_seen", 0)
            replies = stats.get("replies_sent", 0)
            total_posts += posts
            total_replies += replies
            tpl_count = len(self._templates.get(gid, []))
            delay = ch_cfg.reply_delay_seconds
            interval = ch_cfg.reply_interval

            lines.append(
                f"{idx}. {title} ({gid})\n"
                f"   收到帖子: {posts} | 已评论: {replies}\n"
                f"   模板: {tpl_count} 条 | 延时: {delay}s | 间隔: {interval}"
            )

        lines.append("")
        lines.append(f"\U0001f4ca 汇总：收到 {total_posts} 条，评论 {total_replies} 条")

        report_text = "\n".join(lines)

        try:
            await context.bot.send_message(chat_id=stats_channel_id, text=report_text)
            logger.info("[%s] Daily report sent to %d", self.name, stats_channel_id)
        except Exception as e:
            logger.error("[%s] Failed to send daily report: %s", self.name, e)

        # Reset counters
        self._daily_stats.clear()
        self._save_daily_stats()

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "\U0001f44b 你好！我是频道自动评论 Bot。\n\n"
            "我可以在频道消息转发到讨论群后，自动发送你预设的评论模板。\n\n"
            "\U0001f680 三步开始：\n"
            "1. 把我添加到讨论群组并设为管理员\n"
            "2. 私聊我发送 /templates 设置评论内容\n"
            "3. 在频道发消息，我会自动评论\n\n"
            "/templates — 管理评论模板\n"
            "/help — 查看完整帮助\n"
            "/contact — 联系客服"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        await update.message.reply_text(
            "\U0001f4d6 使用帮助\n\n"

            "\U0001f680 快速开始\n"
            "1. 将 Bot 添加到你频道的讨论群组\n"
            "2. 将 Bot 设为群组管理员\n"
            "3. 私聊 Bot 发送 /templates 编辑评论模板\n"
            "4. 在频道发布消息，Bot 会自动在讨论群评论\n\n"

            "\U0001f4dd 模板管理 /templates\n"
            "私聊 Bot 使用，进入后可：\n"
            "  \u2795 添加 — 新增评论模板\n"
            "  \u270f\ufe0f 编辑 — 修改已有模板内容\n"
            "  \U0001f5d1\ufe0f 删除 — 移除不需要的模板\n"
            "  \u2696\ufe0f 权重 — 调整各模板被选中的概率\n"
            "  \u2744\ufe0f 冻结 — 暂停某条模板，不删除\n"
            "  \u23f1 延时 — 设置评论延迟发送的秒数\n"
            "  \U0001f441 预览 — 查看模板实际发送效果\n"
            "模板支持带按钮，用 --- 分隔文字和按钮行\n\n"

            "\u23f1 评论延时\n"
            "在模板管理面板点击「延时」，可调整 Bot 在频道消息出现后等待多少秒再评论，"
            "模拟真人回复节奏。\n\n"

            "\U0001f4e8 联系客服 /contact\n"
            "私聊 Bot 发送 /contact 进入客服模式，"
            "你发送的消息会转达给管理员，管理员的回复也会转发给你。"
            "完成后发 /cancel 退出。\n\n"

            "\U0001f4cb 命令一览\n"
            "/start — 开始使用\n"
            "/templates — 管理评论模板\n"
            "/contact — 联系客服\n"
            "/groups — 查看管理的群组（仅管理员）\n"
            "/clones — 查看已克隆的 Bot（仅管理员）\n"
            "/clone — 克隆 Bot 配置\n"
            "/cancel — 退出当前操作\n"
            "/help — 查看本帮助"
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

    async def _cmd_clones(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /clones — owner-only, show all cloned bot instances."""
        owner_id = self.config.settings.owner_id
        if not update.effective_user or update.effective_user.id != owner_id:
            await update.message.reply_text("\u26a0\ufe0f 仅 Bot 管理员可使用此命令。")
            return

        clones = self._load_clones_meta()
        if not clones:
            await update.message.reply_text("当前没有克隆的 Bot。")
            return

        lines = [f"\U0001f4e6 已克隆的 Bot（共 {len(clones)} 个）\n"]
        for i, (name, info) in enumerate(clones.items(), 1):
            username = info.get("bot_username", "")
            at_name = f"@{username}" if username else name
            cloned_by = info.get("cloned_by_name", "未知")
            cloned_by_id = info.get("cloned_by_id", 0)
            source_title = info.get("source_group_title", "未知")
            source_gid = info.get("source_group_id", "")
            tpl_file = info.get("template_file", "")
            cloned_at = info.get("cloned_at", "")[:10]
            lines.append(
                f"{i}. {at_name}\n"
                f"   克隆人: {cloned_by} ({cloned_by_id})\n"
                f"   来源群组: {source_title} ({source_gid})\n"
                f"   模板文件: {tpl_file}\n"
                f"   克隆时间: {cloned_at}"
            )

        await update.message.reply_text("\n".join(lines))

    # ---- Clone bot configuration ----

    async def _cmd_clone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /clone — private chat, any user who has added the bot to a group."""
        if update.effective_chat.type != "private":
            await update.message.reply_text("\u26a0\ufe0f 请在私聊中使用 /clone 命令。")
            return
        user = update.effective_user
        if not user:
            return

        meta = self._load_groups_meta()

        # Find groups where added_by_id matches the current user
        user_groups = {
            gid: info for gid, info in meta.items()
            if info.get("added_by_id") == user.id
        }

        if not user_groups:
            await update.message.reply_text("你当前没有可克隆的群组。")
            return

        # Only show groups that have templates
        lines = ["\U0001f4e6 克隆 Bot 配置\n\n请选择模板来源群组：\n"]
        buttons = []
        for gid_str, info in user_groups.items():
            gid = int(gid_str)
            tpl_count = len(self._templates.get(gid, []))
            if tpl_count == 0:
                continue
            title = info.get("group_title") or "未知群组"
            lines.append(f"  {title} (ID: {gid}) — {tpl_count} 个模板")
            buttons.append([InlineKeyboardButton(
                text=f"{title} ({tpl_count} 模板)",
                callback_data=f"clone_grp_{gid}",
            )])

        if not buttons:
            await update.message.reply_text("你当前没有可克隆的群组。")
            return

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _cb_clone_group_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle group selection callback for /clone."""
        query = update.callback_query
        await query.answer()

        user = update.effective_user
        if not user:
            return

        gid = int(query.data.replace("clone_grp_", ""))

        # Verify the user is the one who added the bot to this group
        meta = self._load_groups_meta()
        group_info = meta.get(str(gid), {})
        if group_info.get("added_by_id") != user.id:
            await query.edit_message_text("\u26a0\ufe0f 你没有权限克隆该群组。")
            return

        if gid not in self._templates:
            await query.edit_message_text("\u26a0\ufe0f 该群组模板不存在。")
            return

        context.user_data["clone_source_gid"] = gid
        tpl_count = len(self._templates[gid])
        title = group_info.get("group_title") or str(gid)
        await query.edit_message_text(
            f"\u2705 已选择群组「{title}」（{tpl_count} 个模板）\n\n"
            "请发送新 Bot 的 Token（从 @BotFather 获取）\n\n"
            "发送 /cancel 取消操作。"
        )

    async def _handle_clone_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process clone token input — validate via getMe, then clone and hot-start."""
        import httpx
        import os

        token = update.message.text.strip()

        # Basic token format check
        if ":" not in token:
            await update.message.reply_text(
                "\u26a0\ufe0f Token 格式错误，应包含冒号（如 7654321:XYZ...）\n\n"
                "请重新发送 Token，或发 /cancel 取消。"
            )
            return

        gid = context.user_data.get("clone_source_gid")
        if gid is None:
            await update.message.reply_text("\u26a0\ufe0f 克隆流程异常，请重新发 /clone。")
            return

        # Validate token via getMe
        await update.message.reply_text("\u23f3 正在验证 Token...")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
                data = resp.json()
        except Exception as e:
            await update.message.reply_text(f"\u26a0\ufe0f 验证 Token 失败：{e}\n\n请重新发送 Token，或发 /cancel 取消。")
            return

        if not data.get("ok"):
            await update.message.reply_text(
                "\u26a0\ufe0f Token 无效，请检查后重新发送。\n\n"
                "发 /cancel 取消。"
            )
            return

        bot_info = data["result"]
        bot_username = bot_info.get("username", "")
        bot_name = bot_username or bot_info.get("first_name", "cloned_bot")

        # Pop clone_source_gid now that we've validated
        context.user_data.pop("clone_source_gid", None)

        # Read source group templates
        templates = self._templates.get(gid, [])
        if not templates:
            await update.message.reply_text("\u26a0\ufe0f 源群组模板为空，克隆中止。")
            return

        # 1. Write template to templates/{bot_name}.json
        tpl_filename = f"{bot_name}.json"
        tpl_path = Path(__file__).parent / "templates" / tpl_filename
        tpl_data = {
            "templates": [
                {
                    "text": t.text,
                    "weight": t.weight,
                    **({"buttons": t.buttons} if t.buttons else {}),
                    **({"frozen": True} if t.frozen else {}),
                }
                for t in templates
            ]
        }
        tpl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tpl_path, "w", encoding="utf-8") as f:
            json.dump(tpl_data, f, ensure_ascii=False, indent=2)

        # 2. Generate env var name and append to .env
        env_var = f"BOT_{bot_name.upper()}_TOKEN"
        env_path = Path(__file__).parent / ".env"
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\n{env_var}={token}")
        os.environ[env_var] = token

        # 3. Update config.json with new bot entry (including default_template_file)
        config_path = Path(__file__).parent / "config.json"
        with open(config_path, encoding="utf-8") as f:
            config_data = json.load(f)

        new_entry = {
            "name": bot_name,
            "token_env": env_var,
            "default_template_file": tpl_filename,
        }
        config_data.setdefault("bots", []).append(new_entry)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)

        # 4. Build BotConfig and hot-start via manager
        from config import BotConfig, Settings

        bot_settings = Settings(
            default_reply_delay_seconds=self.config.settings.default_reply_delay_seconds,
            default_template_file=tpl_filename,
            log_file=self.config.settings.log_file,
            data_dir=self.config.settings.data_dir,
            owner_id=self.config.settings.owner_id,
            stats_channel_id=self.config.settings.stats_channel_id,
        )
        new_bot_config = BotConfig(
            name=bot_name,
            token=token,
            settings=bot_settings,
        )

        if self._manager:
            try:
                await self._manager.start_bot_dynamic(new_bot_config)
            except Exception as e:
                logger.error("[%s] Failed to hot-start cloned bot %s: %s", self.name, bot_name, e)
                await update.message.reply_text(
                    f"\u26a0\ufe0f 配置已保存但自动启动失败：{e}\n"
                    "请手动重启程序。"
                )
                return
        else:
            logger.warning("[%s] No manager reference, cannot hot-start cloned bot %s", self.name, bot_name)

        # 5. Save clone metadata
        user = update.effective_user
        meta = self._load_groups_meta()
        source_title = meta.get(str(gid), {}).get("group_title") or str(gid)

        clones = self._load_clones_meta()
        clones[bot_name] = {
            "bot_username": bot_username,
            "cloned_by_name": user.full_name if user else "未知",
            "cloned_by_id": user.id if user else 0,
            "source_group_id": gid,
            "source_group_title": source_title,
            "template_file": tpl_filename,
            "cloned_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_clones_meta(clones)

        # 6. Notify owner
        at_username = f"@{bot_username}" if bot_username else bot_name
        owner_id = self.config.settings.owner_id
        if owner_id:
            user_name = user.full_name if user else "未知"
            user_id = user.id if user else 0
            notify_msg = (
                f"\U0001f4e6 新 Bot 克隆通知\n\n"
                f"Bot：{at_username}\n"
                f"操作人：{user_name}（{user_id}）\n"
                f"来源群组：{source_title}（{gid}）\n"
                f"模板文件：{tpl_filename}"
            )
            try:
                await context.bot.send_message(chat_id=owner_id, text=notify_msg)
            except Exception as e:
                logger.warning("[%s] Failed to notify owner about clone: %s", self.name, e)

        await update.message.reply_text(
            f"\u2705 {at_username} 已创建并启动！\n\n"
            f"将新 Bot 添加到你的讨论群组即可使用。"
        )

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
        # Clone input takes priority
        if context.user_data.get("clone_source_gid") is not None:
            await self._handle_clone_input(update, context)
            return
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
        """Handle /cancel — exit contact mode or clone flow (standalone, outside ConversationHandler)."""
        cancelled = False
        if context.user_data.pop("clone_source_gid", None) is not None:
            await update.message.reply_text("\u2705 已取消克隆操作。")
            cancelled = True
        if context.user_data.pop("contact_mode", None):
            await update.message.reply_text("\u2705 已退出客服模式。")
            cancelled = True
        if not cancelled:
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
        app.add_handler(CommandHandler("clones", self._cmd_clones))

        # Register template editor handlers
        register_template_handlers(app, self)

        # Clone bot configuration handlers
        app.add_handler(CommandHandler("clone", self._cmd_clone))
        app.add_handler(CallbackQueryHandler(self._cb_clone_group_select, pattern=r"^clone_grp_-?\d+$"))

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

        # Notify owner on startup
        owner_id = self.config.settings.owner_id
        if owner_id:
            try:
                await self.application.bot.send_message(
                    chat_id=owner_id,
                    text=f"\U0001f7e2 Bot [{self.name}] \u5df2\u542f\u52a8",
                )
            except Exception as e:
                logger.warning("[%s] Failed to send startup notification: %s", self.name, e)

        self._schedule_daily_report()
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
