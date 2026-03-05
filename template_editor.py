"""Template editor module — Inline button interaction for managing comment templates.

Users privately message the bot with /templates, select a group they admin,
then view/add/edit/delete comment templates via inline keyboard buttons.
"""

import json
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Template, load_templates

logger = logging.getLogger(__name__)

# ConversationHandler states
WAITING_TEXT = 0


def save_group_templates(data_dir: str, group_id: int, templates: list[Template]) -> None:
    """Persist templates to data/group_<id>.json."""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"group_{group_id}.json"
    data = {
        "templates": [{"text": t.text, "weight": t.weight} for t in templates],
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Saved %d templates for group %d to %s", len(templates), group_id, file_path)


def load_group_templates(data_dir: str, group_id: int) -> list[Template] | None:
    """Load templates from data/group_<id>.json. Returns None if file does not exist."""
    file_path = Path(data_dir) / f"group_{group_id}.json"
    if not file_path.exists():
        return None
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    templates = []
    for t in data.get("templates", []):
        templates.append(Template(text=t["text"], weight=t.get("weight", 1)))
    return templates if templates else None


def _check_permission(bot, user_id: int, group_id: int) -> bool:
    """Check if user_id has permission to edit templates for group_id.

    Allowed: the person who added the bot to the group, or the bot owner.
    """
    owner_id = bot.config.settings.owner_id
    if user_id == owner_id:
        return True
    meta = bot._load_groups_meta()
    info = meta.get(str(group_id))
    if info and info.get("added_by_id") == user_id:
        return True
    return False


def _get_user_groups(bot, user_id: int) -> dict[int, object]:
    """Return only the groups that user_id is allowed to manage."""
    owner_id = bot.config.settings.owner_id
    if user_id == owner_id:
        return bot._channel_settings

    meta = bot._load_groups_meta()
    allowed = {}
    for gid_str, info in meta.items():
        gid = int(gid_str)
        if info.get("added_by_id") == user_id and gid in bot._channel_settings:
            allowed[gid] = bot._channel_settings[gid]
    return allowed


def _build_template_list_text(templates: list[Template]) -> str:
    """Build the template list display text."""
    lines = [f"\U0001f4cb 评论模板（共 {len(templates)} 条）\n"]
    for i, t in enumerate(templates, 1):
        display_text = t.text if len(t.text) <= 60 else t.text[:57] + "..."
        lines.append(f"{i}. {display_text}  [权重: {t.weight}]")
    return "\n".join(lines)


def _build_main_keyboard() -> InlineKeyboardMarkup:
    """Build the main action keyboard (add/edit/delete/weight)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2795 添加", callback_data="tpl_add"),
            InlineKeyboardButton("\u270f\ufe0f 编辑", callback_data="tpl_edit"),
            InlineKeyboardButton("\U0001f5d1\ufe0f 删除", callback_data="tpl_del"),
        ],
        [
            InlineKeyboardButton("\u2696\ufe0f 权重", callback_data="tpl_weight"),
        ],
    ])


def _build_select_keyboard(templates: list[Template], prefix: str) -> InlineKeyboardMarkup:
    """Build a numbered selection keyboard for edit/delete operations."""
    buttons = []
    row = []
    for i in range(len(templates)):
        label = f"#{i + 1}" if prefix == "tpl_edit" else f"\u274c #{i + 1}"
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_{i}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("\U0001f519 返回", callback_data="tpl_back")])
    return InlineKeyboardMarkup(buttons)


def _build_weight_keyboard(templates: list[Template]) -> InlineKeyboardMarkup:
    """Build a keyboard showing each template with +/- weight buttons."""
    buttons = []
    for i, t in enumerate(templates):
        display = t.text if len(t.text) <= 25 else t.text[:22] + "..."
        buttons.append([
            InlineKeyboardButton(f"#{i + 1} {display} [{t.weight}]", callback_data=f"tpl_wt_noop_{i}"),
        ])
        buttons.append([
            InlineKeyboardButton("\u2796", callback_data=f"tpl_wt_dec_{i}"),
            InlineKeyboardButton(f"权重: {t.weight}", callback_data=f"tpl_wt_noop_{i}"),
            InlineKeyboardButton("\u2795", callback_data=f"tpl_wt_inc_{i}"),
        ])
    buttons.append([InlineKeyboardButton("\U0001f519 返回", callback_data="tpl_back")])
    return InlineKeyboardMarkup(buttons)


def _build_group_select_keyboard(bot, groups: dict[int, object]) -> InlineKeyboardMarkup:
    """Build a keyboard listing all managed groups for selection."""
    meta = bot._load_groups_meta()
    buttons = []
    for gid in groups:
        title = meta.get(str(gid), {}).get("group_title") or f"群组 {gid}"
        buttons.append([InlineKeyboardButton(
            title, callback_data=f"tpl_grp_{gid}"
        )])
    return InlineKeyboardMarkup(buttons)


def register_template_handlers(app: Application, bot) -> None:
    """Register all template editor handlers on the application."""

    async def cmd_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /templates command — only in private chat, show group selection."""
        if update.effective_chat.type != "private":
            await update.message.reply_text("请在与 Bot 的私聊中使用 /templates 命令。")
            return

        user_id = update.effective_user.id
        groups = _get_user_groups(bot, user_id)
        if not groups:
            await update.message.reply_text("\u26a0\ufe0f 你没有可管理的群组。")
            return

        # If only one group, skip selection
        if len(groups) == 1:
            gid = next(iter(groups))
            context.user_data["tpl_group_id"] = gid
            templates = bot._templates.get(gid, [])
            if not templates:
                await update.message.reply_text("该群组没有配置模板。")
                return
            meta = bot._load_groups_meta()
            title = meta.get(str(gid), {}).get("group_title") or f"群组 {gid}"
            text = f"{title}\n\n" + _build_template_list_text(templates)
            await update.message.reply_text(text, reply_markup=_build_main_keyboard())
            return

        await update.message.reply_text(
            "请选择要编辑模板的群组：",
            reply_markup=_build_group_select_keyboard(bot, groups),
        )

    async def cb_group_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_grp_<id> callback — select a group and show its templates."""
        query = update.callback_query
        await query.answer()

        gid = int(query.data.split("tpl_grp_")[1])
        user_id = update.effective_user.id

        if not _check_permission(bot, user_id, gid):
            await query.edit_message_text("\u26a0\ufe0f 你没有该群组的编辑权限。")
            return

        context.user_data["tpl_group_id"] = gid
        templates = bot._templates.get(gid, [])
        if not templates:
            await query.edit_message_text("该群组没有配置模板。")
            return

        meta = bot._load_groups_meta()
        title = meta.get(str(gid), {}).get("group_title") or f"群组 {gid}"
        text = f"{title}\n\n" + _build_template_list_text(templates)
        await query.edit_message_text(text, reply_markup=_build_main_keyboard())

    def _get_group_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
        """Get the selected group_id from user_data."""
        return context.user_data.get("tpl_group_id")

    async def cb_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle tpl_add callback — ask user for new template text."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return ConversationHandler.END

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.edit_message_text("\u26a0\ufe0f 你没有该群组的编辑权限。")
            return ConversationHandler.END

        context.user_data["tpl_action"] = "add"
        await query.edit_message_text("请发送新的评论模板内容：\n\n发送 /cancel 取消操作")
        return WAITING_TEXT

    async def cb_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_edit callback — show numbered buttons for selection."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.edit_message_text("\u26a0\ufe0f 你没有该群组的编辑权限。")
            return

        templates = bot._templates.get(gid, [])
        if not templates:
            await query.edit_message_text("没有可编辑的模板。")
            return

        text = _build_template_list_text(templates) + "\n\n请选择要编辑的模板编号："
        await query.edit_message_text(text, reply_markup=_build_select_keyboard(templates, "tpl_edit"))

    async def cb_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle tpl_edit_N callback — ask for new text for template #N."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return ConversationHandler.END

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.edit_message_text("\u26a0\ufe0f 你没有该群组的编辑权限。")
            return ConversationHandler.END

        idx = int(query.data.split("_")[-1])
        templates = bot._templates.get(gid, [])

        if idx < 0 or idx >= len(templates):
            await query.edit_message_text("\u26a0\ufe0f 无效的模板编号。")
            return ConversationHandler.END

        context.user_data["tpl_action"] = "edit"
        context.user_data["tpl_edit_index"] = idx

        current = templates[idx].text
        await query.edit_message_text(
            f"请发送模板 #{idx + 1} 的新内容（当前：{current}）：\n\n发送 /cancel 取消操作"
        )
        return WAITING_TEXT

    async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle text message during add/edit conversation."""
        text = update.message.text.strip()
        if not text:
            await update.message.reply_text("模板内容不能为空，请重新发送：")
            return WAITING_TEXT

        action = context.user_data.get("tpl_action")
        gid = _get_group_id(context)
        if not gid:
            await update.message.reply_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return ConversationHandler.END

        templates = bot._templates.get(gid, [])
        data_dir = bot.config.settings.data_dir

        if action == "add":
            templates.append(Template(text=text, weight=1))
            bot._templates[gid] = templates
            save_group_templates(data_dir, gid, templates)
            msg = f"\u2705 已添加模板 #{len(templates)}"

        elif action == "edit":
            idx = context.user_data.get("tpl_edit_index", 0)
            if 0 <= idx < len(templates):
                templates[idx] = Template(text=text, weight=templates[idx].weight)
                bot._templates[gid] = templates
                save_group_templates(data_dir, gid, templates)
                msg = f"\u2705 已更新模板 #{idx + 1}"
            else:
                msg = "\u26a0\ufe0f 无效的模板编号。"
        else:
            msg = "\u26a0\ufe0f 未知操作。"

        # Clear action state (keep tpl_group_id for continued editing)
        for key in ("tpl_action", "tpl_edit_index"):
            context.user_data.pop(key, None)

        # Show updated list
        list_text = _build_template_list_text(bot._templates.get(gid, []))
        await update.message.reply_text(f"{msg}\n\n{list_text}", reply_markup=_build_main_keyboard())
        return ConversationHandler.END

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle /cancel — exit the conversation."""
        for key in ("tpl_action", "tpl_group_id", "tpl_edit_index"):
            context.user_data.pop(key, None)
        await update.message.reply_text("已取消操作。")
        return ConversationHandler.END

    async def cb_delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_del callback — show delete buttons for each template."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.edit_message_text("\u26a0\ufe0f 你没有该群组的编辑权限。")
            return

        templates = bot._templates.get(gid, [])
        if not templates:
            await query.edit_message_text("没有可删除的模板。")
            return

        text = _build_template_list_text(templates) + "\n\n请选择要删除的模板："
        await query.edit_message_text(text, reply_markup=_build_select_keyboard(templates, "tpl_del"))

    async def cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_del_N callback — delete template #N."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.edit_message_text("\u26a0\ufe0f 你没有该群组的编辑权限。")
            return

        idx = int(query.data.split("_")[-1])
        templates = bot._templates.get(gid, [])

        if idx < 0 or idx >= len(templates):
            await query.edit_message_text("\u26a0\ufe0f 无效的模板编号。")
            return

        removed = templates.pop(idx)
        bot._templates[gid] = templates

        data_dir = bot.config.settings.data_dir
        save_group_templates(data_dir, gid, templates)

        msg = f"\u2705 已删除模板 #{idx + 1}（{removed.text[:30]}...）"

        if templates:
            list_text = _build_template_list_text(templates)
            await query.edit_message_text(f"{msg}\n\n{list_text}", reply_markup=_build_main_keyboard())
        else:
            await query.edit_message_text(f"{msg}\n\n当前没有模板。", reply_markup=_build_main_keyboard())

    async def cb_weight_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_weight callback — show weight adjustment UI."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.edit_message_text("\u26a0\ufe0f 你没有该群组的编辑权限。")
            return

        templates = bot._templates.get(gid, [])
        if not templates:
            await query.edit_message_text("没有可调整权重的模板。")
            return

        total = sum(t.weight for t in templates)
        text = f"\u2696\ufe0f 调整模板权重（权重总和: {total}）\n\n点击 +/- 调整各模板的选中概率："
        await query.edit_message_text(text, reply_markup=_build_weight_keyboard(templates))

    async def cb_weight_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_wt_inc_N / tpl_wt_dec_N callbacks — adjust weight."""
        query = update.callback_query

        gid = _get_group_id(context)
        if not gid:
            await query.answer("\u26a0\ufe0f 请先使用 /templates 选择群组。", show_alert=True)
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.answer("\u26a0\ufe0f 你没有该群组的编辑权限。", show_alert=True)
            return

        parts = query.data.split("_")  # tpl_wt_inc_0 or tpl_wt_dec_0
        action = parts[2]  # inc or dec
        idx = int(parts[3])
        templates = bot._templates.get(gid, [])

        if idx < 0 or idx >= len(templates):
            await query.answer("\u26a0\ufe0f 无效的模板编号。", show_alert=True)
            return

        t = templates[idx]
        if action == "inc":
            templates[idx] = Template(text=t.text, weight=t.weight + 1)
        elif action == "dec" and t.weight > 1:
            templates[idx] = Template(text=t.text, weight=t.weight - 1)
        else:
            await query.answer("权重最小为 1", show_alert=True)
            return

        bot._templates[gid] = templates
        data_dir = bot.config.settings.data_dir
        save_group_templates(data_dir, gid, templates)

        total = sum(tp.weight for tp in templates)
        text = f"\u2696\ufe0f 调整模板权重（权重总和: {total}）\n\n点击 +/- 调整各模板的选中概率："
        await query.answer()
        await query.edit_message_text(text, reply_markup=_build_weight_keyboard(templates))

    async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_back callback — return to main template list."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return

        templates = bot._templates.get(gid, [])
        if templates:
            text = _build_template_list_text(templates)
            await query.edit_message_text(text, reply_markup=_build_main_keyboard())
        else:
            await query.edit_message_text("当前没有模板。", reply_markup=_build_main_keyboard())

    # Build ConversationHandler for add/edit text input
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_add_entry, pattern=r"^tpl_add$"),
            CallbackQueryHandler(cb_edit_entry, pattern=r"^tpl_edit_\d+$"),
        ],
        states={
            WAITING_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    # Register handlers
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("templates", cmd_templates))
    app.add_handler(CallbackQueryHandler(cb_group_select, pattern=r"^tpl_grp_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_edit_select, pattern=r"^tpl_edit$"))
    app.add_handler(CallbackQueryHandler(cb_delete_select, pattern=r"^tpl_del$"))
    app.add_handler(CallbackQueryHandler(cb_delete_confirm, pattern=r"^tpl_del_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_weight_select, pattern=r"^tpl_weight$"))
    app.add_handler(CallbackQueryHandler(cb_weight_adjust, pattern=r"^tpl_wt_(inc|dec)_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_back, pattern=r"^tpl_back$"))

    logger.info("Template editor handlers registered")
