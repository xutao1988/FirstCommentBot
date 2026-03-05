"""Template editor module — Inline button interaction for managing comment templates.

Provides /templates command + inline keyboard for group admins to view/add/edit/delete
comment templates directly within Telegram.
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


async def _check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the user is a group admin or creator. Returns True if admin."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False

    # Private chats — allow (for testing / single-user scenarios)
    if chat.type == "private":
        return True

    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


def _build_template_list_text(templates: list[Template]) -> str:
    """Build the template list display text."""
    lines = [f"\U0001f4cb 评论模板（共 {len(templates)} 条）\n"]
    for i, t in enumerate(templates, 1):
        # Truncate long templates for display
        display_text = t.text if len(t.text) <= 60 else t.text[:57] + "..."
        lines.append(f"{i}. {display_text}  [权重: {t.weight}]")
    return "\n".join(lines)


def _build_main_keyboard() -> InlineKeyboardMarkup:
    """Build the main action keyboard (add/edit/delete)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2795 添加", callback_data="tpl_add"),
            InlineKeyboardButton("\u270f\ufe0f 编辑", callback_data="tpl_edit"),
            InlineKeyboardButton("\U0001f5d1\ufe0f 删除", callback_data="tpl_del"),
        ]
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


def register_template_handlers(app: Application, bot) -> None:
    """Register all template editor handlers on the application."""

    async def cmd_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /templates command — show template list with action buttons."""
        if not await _check_admin(update, context):
            await update.message.reply_text("\u26a0\ufe0f 仅群组管理员可编辑模板")
            return

        chat_id = update.effective_chat.id
        templates = bot._templates.get(chat_id)
        if not templates:
            await update.message.reply_text("当前群组没有配置模板。")
            return

        text = _build_template_list_text(templates)
        await update.message.reply_text(text, reply_markup=_build_main_keyboard())

    async def cb_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle tpl_add callback — ask user for new template text."""
        query = update.callback_query
        await query.answer()

        if not await _check_admin(update, context):
            await query.answer("\u26a0\ufe0f 仅群组管理员可编辑模板", show_alert=True)
            return ConversationHandler.END

        chat_id = update.effective_chat.id
        context.user_data["tpl_action"] = "add"
        context.user_data["tpl_group_id"] = chat_id

        await query.edit_message_text("请发送新的评论模板内容：\n\n发送 /cancel 取消操作")
        return WAITING_TEXT

    async def cb_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_edit callback — show numbered buttons for selection."""
        query = update.callback_query
        await query.answer()

        if not await _check_admin(update, context):
            await query.answer("\u26a0\ufe0f 仅群组管理员可编辑模板", show_alert=True)
            return

        chat_id = update.effective_chat.id
        templates = bot._templates.get(chat_id, [])
        if not templates:
            await query.edit_message_text("没有可编辑的模板。")
            return

        text = _build_template_list_text(templates) + "\n\n请选择要编辑的模板编号："
        await query.edit_message_text(text, reply_markup=_build_select_keyboard(templates, "tpl_edit"))

    async def cb_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle tpl_edit_N callback — ask for new text for template #N."""
        query = update.callback_query
        await query.answer()

        if not await _check_admin(update, context):
            await query.answer("\u26a0\ufe0f 仅群组管理员可编辑模板", show_alert=True)
            return ConversationHandler.END

        chat_id = update.effective_chat.id
        idx = int(query.data.split("_")[-1])
        templates = bot._templates.get(chat_id, [])

        if idx < 0 or idx >= len(templates):
            await query.edit_message_text("\u26a0\ufe0f 无效的模板编号。")
            return ConversationHandler.END

        context.user_data["tpl_action"] = "edit"
        context.user_data["tpl_group_id"] = chat_id
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
        group_id = context.user_data.get("tpl_group_id")
        templates = bot._templates.get(group_id, [])

        data_dir = bot.config.settings.data_dir

        if action == "add":
            templates.append(Template(text=text, weight=1))
            bot._templates[group_id] = templates
            save_group_templates(data_dir, group_id, templates)
            msg = f"\u2705 已添加模板 #{len(templates)}"

        elif action == "edit":
            idx = context.user_data.get("tpl_edit_index", 0)
            if 0 <= idx < len(templates):
                templates[idx] = Template(text=text, weight=templates[idx].weight)
                bot._templates[group_id] = templates
                save_group_templates(data_dir, group_id, templates)
                msg = f"\u2705 已更新模板 #{idx + 1}"
            else:
                msg = "\u26a0\ufe0f 无效的模板编号。"
        else:
            msg = "\u26a0\ufe0f 未知操作。"

        # Clear user_data
        for key in ("tpl_action", "tpl_group_id", "tpl_edit_index"):
            context.user_data.pop(key, None)

        # Show updated list
        list_text = _build_template_list_text(bot._templates.get(group_id, []))
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

        if not await _check_admin(update, context):
            await query.answer("\u26a0\ufe0f 仅群组管理员可编辑模板", show_alert=True)
            return

        chat_id = update.effective_chat.id
        templates = bot._templates.get(chat_id, [])
        if not templates:
            await query.edit_message_text("没有可删除的模板。")
            return

        text = _build_template_list_text(templates) + "\n\n请选择要删除的模板："
        await query.edit_message_text(text, reply_markup=_build_select_keyboard(templates, "tpl_del"))

    async def cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_del_N callback — delete template #N."""
        query = update.callback_query
        await query.answer()

        if not await _check_admin(update, context):
            await query.answer("\u26a0\ufe0f 仅群组管理员可编辑模板", show_alert=True)
            return

        chat_id = update.effective_chat.id
        idx = int(query.data.split("_")[-1])
        templates = bot._templates.get(chat_id, [])

        if idx < 0 or idx >= len(templates):
            await query.edit_message_text("\u26a0\ufe0f 无效的模板编号。")
            return

        removed = templates.pop(idx)
        bot._templates[chat_id] = templates

        data_dir = bot.config.settings.data_dir
        save_group_templates(data_dir, chat_id, templates)

        msg = f"\u2705 已删除模板 #{idx + 1}（{removed.text[:30]}...）"

        if templates:
            list_text = _build_template_list_text(templates)
            await query.edit_message_text(
                f"{msg}\n\n{list_text}", reply_markup=_build_main_keyboard()
            )
        else:
            await query.edit_message_text(f"{msg}\n\n当前没有模板。", reply_markup=_build_main_keyboard())

    async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_back callback — return to main template list."""
        query = update.callback_query
        await query.answer()

        chat_id = update.effective_chat.id
        templates = bot._templates.get(chat_id, [])
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
    )

    # Register handlers — ConversationHandler first (higher priority)
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("templates", cmd_templates))
    app.add_handler(CallbackQueryHandler(cb_edit_select, pattern=r"^tpl_edit$"))
    app.add_handler(CallbackQueryHandler(cb_delete_select, pattern=r"^tpl_del$"))
    app.add_handler(CallbackQueryHandler(cb_delete_confirm, pattern=r"^tpl_del_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_back, pattern=r"^tpl_back$"))

    logger.info("Template editor handlers registered")
