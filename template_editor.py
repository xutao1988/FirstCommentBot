"""Template editor module — Inline button interaction for managing comment templates.

Users privately message the bot with /templates, select a group they admin,
then view/add/edit/delete comment templates via inline keyboard buttons.
"""

import json
import logging
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Template, _normalize_buttons, load_templates

logger = logging.getLogger(__name__)

# ConversationHandler states
WAITING_TEXT = 0

# Template limits per group
FREE_TEMPLATE_LIMIT = 3

# Color name mapping: Chinese/English → Bot API style
_COLOR_MAP = {
    "红": "danger", "red": "danger", "danger": "danger",
    "蓝": "primary", "blue": "primary", "primary": "primary",
    "绿": "success", "green": "success", "success": "success",
}

# Reverse mapping for display
_STYLE_TO_CN = {"danger": "红", "primary": "蓝", "success": "绿"}

_BTN_RE = re.compile(r"\[(.+?)]\((.+?)\)(?:\{(.+?)})?")  # findall per line


def save_group_templates(data_dir: str, group_id: int, templates: list[Template]) -> None:
    """Persist templates to data/group_<id>.json."""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"group_{group_id}.json"
    data = {
        "templates": [
            {
                "text": t.text,
                "weight": t.weight,
                **({"buttons": t.buttons} if t.buttons else {}),
                **({"frozen": True} if t.frozen else {}),
                **({"media_file_id": t.media_file_id} if t.media_file_id else {}),
                **({"media_type": t.media_type} if t.media_type else {}),
            }
            for t in templates
        ],
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
        templates.append(Template(
            text=t["text"],
            weight=t.get("weight", 1),
            buttons=_normalize_buttons(t.get("buttons", [])),
            frozen=t.get("frozen", False),
            media_file_id=t.get("media_file_id", ""),
            media_type=t.get("media_type", ""),
        ))
    return templates if templates else None


def _parse_template_input(raw: str) -> tuple[str, list[list[dict]]]:
    """Parse user input with optional --- button section.

    Format:
        模板文本
        ---
        [按钮A](URL){颜色} [按钮B](URL)   ← 同一行 = 并排
        [按钮C](URL)                       ← 新行 = 新行

    Returns (text, buttons) where buttons is a list of rows.
    """
    if "---" not in raw:
        return raw.strip(), []

    text_part, btn_part = raw.split("---", 1)
    text = text_part.strip()
    rows = []
    for line in btn_part.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        matches = _BTN_RE.findall(line)
        if not matches:
            continue
        row = []
        for btn_text, btn_url, color in matches:
            btn = {"text": btn_text, "url": btn_url}
            if color:
                style = _COLOR_MAP.get(color.strip().lower(), _COLOR_MAP.get(color.strip()))
                if style:
                    btn["style"] = style
            row.append(btn)
        rows.append(row)
    return text, rows


def _format_template_for_edit(template: Template) -> str:
    """Reconstruct the text + --- + buttons format for editing."""
    if not template.buttons:
        return template.text
    lines = [template.text, "---"]
    for row in template.buttons:
        parts = []
        for btn in row:
            s = f"[{btn['text']}]({btn['url']})"
            cn = _STYLE_TO_CN.get(btn.get("style", ""))
            if cn:
                s += f"{{{cn}}}"
            parts.append(s)
        lines.append(" ".join(parts))
    return "\n".join(lines)


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
    active_count = sum(1 for t in templates if not t.frozen)
    frozen_count = len(templates) - active_count
    header = f"\U0001f4cb 评论模板（共 {len(templates)} 条"
    if frozen_count:
        header += f"，{frozen_count} 条已冻结"
    header += "）\n"
    lines = [header]
    for i, t in enumerate(templates, 1):
        prefix = "\u2744\ufe0f " if t.frozen else ""
        display_text = t.text if len(t.text) <= 60 else t.text[:57] + "..."
        suffix = f"  [权重: {t.weight}]"
        if t.buttons:
            btn_total = sum(len(row) for row in t.buttons)
            suffix += f" [按钮: {btn_total}]"
        if t.frozen:
            suffix += " [已冻结]"
        if t.media_type == "photo":
            suffix += " [图片]"
        elif t.media_type == "animation":
            suffix += " [GIF]"
        elif t.media_type == "video":
            suffix += " [视频]"
        lines.append(f"{i}. {prefix}{display_text}{suffix}")
    return "\n".join(lines)


def _build_main_keyboard(num_templates: int = 0) -> InlineKeyboardMarkup:
    """Build the main action keyboard (add/edit/delete/weight/freeze + preview)."""
    rows = [
        [
            InlineKeyboardButton("\u2795 添加", callback_data="tpl_add"),
            InlineKeyboardButton("\u270f\ufe0f 编辑", callback_data="tpl_edit"),
            InlineKeyboardButton("\U0001f5d1\ufe0f 删除", callback_data="tpl_del"),
        ],
        [
            InlineKeyboardButton("\u2696\ufe0f 权重", callback_data="tpl_weight"),
            InlineKeyboardButton("\u2744\ufe0f 冻结", callback_data="tpl_freeze"),
            InlineKeyboardButton("\u23f1 延时", callback_data="tpl_delay"),
            InlineKeyboardButton("\U0001f4ca 间隔", callback_data="tpl_interval"),
        ],
    ]
    if num_templates > 0:
        rows.append([InlineKeyboardButton("\U0001f441 预览模板", callback_data="tpl_preview")])
    return InlineKeyboardMarkup(rows)


def _build_select_keyboard(templates: list[Template], prefix: str) -> InlineKeyboardMarkup:
    """Build a numbered selection keyboard for edit/delete operations."""
    buttons = []
    row = []
    for i in range(len(templates)):
        if prefix == "tpl_del":
            label = f"\u274c #{i + 1}"
        else:
            label = f"#{i + 1}"
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


def _build_freeze_keyboard(templates: list[Template]) -> InlineKeyboardMarkup:
    """Build a keyboard showing each template with a freeze/unfreeze toggle."""
    buttons = []
    for i, t in enumerate(templates):
        display = t.text if len(t.text) <= 25 else t.text[:22] + "..."
        icon = "\u2744\ufe0f" if t.frozen else "\u2600\ufe0f"
        status = "已冻结" if t.frozen else "活跃"
        buttons.append([InlineKeyboardButton(
            f"{icon} #{i + 1} {display} [{status}]",
            callback_data=f"tpl_frz_{i}",
        )])
    buttons.append([InlineKeyboardButton("\U0001f519 返回", callback_data="tpl_back")])
    return InlineKeyboardMarkup(buttons)


def _build_delay_keyboard(current_delay: int) -> InlineKeyboardMarkup:
    """Build a keyboard for adjusting reply delay (seconds)."""
    buttons = [
        [
            InlineKeyboardButton("\u2796 1s", callback_data="tpl_dly_dec_1"),
            InlineKeyboardButton(f"\u23f1 {current_delay}s", callback_data="tpl_dly_noop"),
            InlineKeyboardButton("\u2795 1s", callback_data="tpl_dly_inc_1"),
        ],
        [
            InlineKeyboardButton("\u2796 5s", callback_data="tpl_dly_dec_5"),
            InlineKeyboardButton("\u2795 5s", callback_data="tpl_dly_inc_5"),
        ],
        [
            InlineKeyboardButton("\U0001f519 返回", callback_data="tpl_back"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def _build_interval_keyboard(current_interval: int) -> InlineKeyboardMarkup:
    """Build a keyboard for adjusting reply interval (every N posts)."""
    buttons = [
        [
            InlineKeyboardButton("\u2796 1", callback_data="tpl_itv_dec_1"),
            InlineKeyboardButton(f"\U0001f4ca {current_interval}", callback_data="tpl_itv_noop"),
            InlineKeyboardButton("\u2795 1", callback_data="tpl_itv_inc_1"),
        ],
        [
            InlineKeyboardButton("\u2796 5", callback_data="tpl_itv_dec_5"),
            InlineKeyboardButton("\u2795 5", callback_data="tpl_itv_inc_5"),
        ],
        [
            InlineKeyboardButton("\U0001f519 返回", callback_data="tpl_back"),
        ],
    ]
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
            await update.message.reply_text(text, reply_markup=_build_main_keyboard(len(templates)))
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
        await query.edit_message_text(text, reply_markup=_build_main_keyboard(len(templates)))

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

        # Enforce template limit for non-owner users
        owner_id = bot.config.settings.owner_id
        templates = bot._templates.get(gid, [])
        if user_id != owner_id and len(templates) >= FREE_TEMPLATE_LIMIT:
            await query.edit_message_text(
                f"\u26a0\ufe0f 免费用户每个群组最多 {FREE_TEMPLATE_LIMIT} 条模板。\n"
                "升级 Pro 可解锁更多模板数量。"
            )
            return ConversationHandler.END

        context.user_data["tpl_action"] = "add"
        await query.edit_message_text(
            "请发送新的评论模板内容：\n\n"
            "如需添加 URL 按钮，用 --- 分隔，例如：\n"
            "沙发已备好\n"
            "---\n"
            "[加入频道](https://t.me/xxx){蓝}\n"
            "[官网](https://example.com)\n\n"
            "同一行写多个按钮即并排显示：\n"
            "[频道](https://t.me/xxx){蓝} [官网](https://example.com)\n\n"
            "颜色可选：{红} {蓝} {绿}，不写则默认透明\n\n"
            "也可直接发送 图片/GIF/视频（附文字说明）创建媒体模板\n\n"
            "发送 /cancel 取消操作"
        )
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

        current = _format_template_for_edit(templates[idx])
        media_hint = ""
        t = templates[idx]
        if t.media_file_id and t.media_type:
            media_label = {"photo": "图片", "animation": "GIF", "video": "视频"}.get(t.media_type, "媒体")
            media_hint = f"\n当前附带[{media_label}]，发送新媒体可替换，发送纯文字则移除媒体\n"
        await query.edit_message_text(
            f"当前内容：\n{current}\n{media_hint}\n"
            "请发送新内容（可用 --- 分隔按钮）：\n"
            "也可直接发送 图片/GIF/视频（附文字说明）\n\n"
            "发送 /cancel 取消操作"
        )
        return WAITING_TEXT

    async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle text message during add/edit conversation.

        Parses --- separator for inline button definitions.
        """
        raw = update.message.text.strip()
        if not raw:
            await update.message.reply_text("模板内容不能为空，请重新发送：")
            return WAITING_TEXT

        text, buttons = _parse_template_input(raw)
        if not text:
            await update.message.reply_text("模板文本不能为空，请重新发送：")
            return WAITING_TEXT

        action = context.user_data.get("tpl_action")
        gid = _get_group_id(context)
        if not gid:
            await update.message.reply_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return ConversationHandler.END

        templates = bot._templates.get(gid, [])
        data_dir = bot.config.settings.data_dir

        if action == "add":
            templates.append(Template(text=text, weight=1, buttons=buttons))
            bot._templates[gid] = templates
            save_group_templates(data_dir, gid, templates)
            btn_total = sum(len(r) for r in buttons)
            btn_info = f"（含 {btn_total} 个按钮）" if btn_total else ""
            msg = f"\u2705 已添加模板 #{len(templates)}{btn_info}"

        elif action == "edit":
            idx = context.user_data.get("tpl_edit_index", 0)
            if 0 <= idx < len(templates):
                templates[idx] = Template(text=text, weight=templates[idx].weight, buttons=buttons, frozen=templates[idx].frozen)
                bot._templates[gid] = templates
                save_group_templates(data_dir, gid, templates)
                btn_total = sum(len(r) for r in buttons)
                btn_info = f"（含 {btn_total} 个按钮）" if btn_total else ""
                msg = f"\u2705 已更新模板 #{idx + 1}{btn_info}"
            else:
                msg = "\u26a0\ufe0f 无效的模板编号。"
        else:
            msg = "\u26a0\ufe0f 未知操作。"

        # Clear action state (keep tpl_group_id for continued editing)
        for key in ("tpl_action", "tpl_edit_index"):
            context.user_data.pop(key, None)

        # Show updated list
        updated = bot._templates.get(gid, [])
        list_text = _build_template_list_text(updated)
        await update.message.reply_text(f"{msg}\n\n{list_text}", reply_markup=_build_main_keyboard(len(updated)))
        return ConversationHandler.END

    async def handle_media_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle photo/GIF/video message during add/edit conversation."""
        message = update.message

        # Determine media type and file_id
        if message.photo:
            media_file_id = message.photo[-1].file_id
            media_type = "photo"
        elif message.animation:
            media_file_id = message.animation.file_id
            media_type = "animation"
        elif message.video:
            media_file_id = message.video.file_id
            media_type = "video"
        else:
            await message.reply_text("\u26a0\ufe0f 不支持的媒体类型，请发送图片、GIF 或视频。")
            return WAITING_TEXT

        # Caption is the text content
        raw = (message.caption or "").strip()
        if not raw:
            await message.reply_text("媒体模板需要添加文字说明，请重新发送（附带 caption）：")
            return WAITING_TEXT

        text, buttons = _parse_template_input(raw)
        if not text:
            await message.reply_text("模板文本不能为空，请重新发送：")
            return WAITING_TEXT

        action = context.user_data.get("tpl_action")
        gid = _get_group_id(context)
        if not gid:
            await message.reply_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return ConversationHandler.END

        templates = bot._templates.get(gid, [])
        data_dir = bot.config.settings.data_dir

        media_label = {"photo": "图片", "animation": "GIF", "video": "视频"}.get(media_type, "媒体")

        if action == "add":
            templates.append(Template(
                text=text, weight=1, buttons=buttons,
                media_file_id=media_file_id, media_type=media_type,
            ))
            bot._templates[gid] = templates
            save_group_templates(data_dir, gid, templates)
            msg = f"\u2705 已添加媒体模板 #{len(templates)} [{media_label}]"

        elif action == "edit":
            idx = context.user_data.get("tpl_edit_index", 0)
            if 0 <= idx < len(templates):
                templates[idx] = Template(
                    text=text, weight=templates[idx].weight, buttons=buttons,
                    frozen=templates[idx].frozen,
                    media_file_id=media_file_id, media_type=media_type,
                )
                bot._templates[gid] = templates
                save_group_templates(data_dir, gid, templates)
                msg = f"\u2705 已更新模板 #{idx + 1} [{media_label}]"
            else:
                msg = "\u26a0\ufe0f 无效的模板编号。"
        else:
            msg = "\u26a0\ufe0f 未知操作。"

        # Clear action state
        for key in ("tpl_action", "tpl_edit_index"):
            context.user_data.pop(key, None)

        updated = bot._templates.get(gid, [])
        list_text = _build_template_list_text(updated)
        await message.reply_text(f"{msg}\n\n{list_text}", reply_markup=_build_main_keyboard(len(updated)))
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
            await query.edit_message_text(f"{msg}\n\n{list_text}", reply_markup=_build_main_keyboard(len(templates)))
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
            templates[idx] = Template(text=t.text, weight=t.weight + 1, buttons=t.buttons, frozen=t.frozen, media_file_id=t.media_file_id, media_type=t.media_type)
        elif action == "dec" and t.weight > 1:
            templates[idx] = Template(text=t.text, weight=t.weight - 1, buttons=t.buttons, frozen=t.frozen, media_file_id=t.media_file_id, media_type=t.media_type)
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

    async def cb_freeze_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_freeze callback — show freeze toggle list."""
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
            await query.edit_message_text("没有可操作的模板。")
            return

        active = sum(1 for t in templates if not t.frozen)
        text = f"\u2744\ufe0f 冻结管理（活跃: {active} / 总计: {len(templates)}）\n\n点击切换冻结状态："
        await query.edit_message_text(text, reply_markup=_build_freeze_keyboard(templates))

    async def cb_freeze_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_frz_N callback — toggle frozen state for template #N."""
        query = update.callback_query

        gid = _get_group_id(context)
        if not gid:
            await query.answer("\u26a0\ufe0f 请先使用 /templates 选择群组。", show_alert=True)
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.answer("\u26a0\ufe0f 你没有该群组的编辑权限。", show_alert=True)
            return

        idx = int(query.data.split("_")[-1])
        templates = bot._templates.get(gid, [])

        if idx < 0 or idx >= len(templates):
            await query.answer("\u26a0\ufe0f 无效的模板编号。", show_alert=True)
            return

        t = templates[idx]
        templates[idx] = Template(text=t.text, weight=t.weight, buttons=t.buttons, frozen=not t.frozen, media_file_id=t.media_file_id, media_type=t.media_type)
        bot._templates[gid] = templates

        data_dir = bot.config.settings.data_dir
        save_group_templates(data_dir, gid, templates)

        new_state = "冻结" if templates[idx].frozen else "解冻"
        await query.answer(f"模板 #{idx + 1} 已{new_state}")

        active = sum(1 for tp in templates if not tp.frozen)
        text = f"\u2744\ufe0f 冻结管理（活跃: {active} / 总计: {len(templates)}）\n\n点击切换冻结状态："
        await query.edit_message_text(text, reply_markup=_build_freeze_keyboard(templates))

    async def cb_delay_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_delay callback — show delay adjustment UI."""
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

        ch = bot._channel_settings.get(gid)
        current = ch.reply_delay_seconds if ch else 0
        text = f"\u23f1 评论延时设置\n\n当前延时：{current} 秒\n评论将在频道消息转发后等待此时间再发送。"
        await query.edit_message_text(text, reply_markup=_build_delay_keyboard(current))

    async def cb_delay_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_dly_inc/dec callbacks — adjust reply delay."""
        query = update.callback_query

        gid = _get_group_id(context)
        if not gid:
            await query.answer("\u26a0\ufe0f 请先使用 /templates 选择群组。", show_alert=True)
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.answer("\u26a0\ufe0f 你没有该群组的编辑权限。", show_alert=True)
            return

        ch = bot._channel_settings.get(gid)
        if not ch:
            await query.answer("\u26a0\ufe0f 群组配置不存在。", show_alert=True)
            return

        # Parse: tpl_dly_inc_5 or tpl_dly_dec_1
        parts = query.data.split("_")  # ['tpl', 'dly', 'inc'/'dec', '1'/'5']
        action = parts[2]
        step = int(parts[3])

        current = ch.reply_delay_seconds
        if action == "inc":
            new_delay = current + step
        else:
            new_delay = max(0, current - step)

        ch.reply_delay_seconds = new_delay

        # Persist to groups_meta.json
        meta = bot._load_groups_meta()
        if str(gid) in meta:
            meta[str(gid)]["reply_delay_seconds"] = new_delay
            bot._save_groups_meta(meta)

        await query.answer(f"延时已设为 {new_delay} 秒")

        text = f"\u23f1 评论延时设置\n\n当前延时：{new_delay} 秒\n评论将在频道消息转发后等待此时间再发送。"
        await query.edit_message_text(text, reply_markup=_build_delay_keyboard(new_delay))

    async def cb_interval_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_interval callback — show interval adjustment UI."""
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

        ch = bot._channel_settings.get(gid)
        current = ch.reply_interval if ch else 1
        text = f"\U0001f4ca 评论间隔设置\n\n当前间隔：每 {current} 条帖子评论一次\n设为 1 表示每条都评论。"
        await query.edit_message_text(text, reply_markup=_build_interval_keyboard(current))

    async def cb_interval_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_itv_inc/dec callbacks — adjust reply interval."""
        query = update.callback_query

        gid = _get_group_id(context)
        if not gid:
            await query.answer("\u26a0\ufe0f 请先使用 /templates 选择群组。", show_alert=True)
            return

        user_id = update.effective_user.id
        if not _check_permission(bot, user_id, gid):
            await query.answer("\u26a0\ufe0f 你没有该群组的编辑权限。", show_alert=True)
            return

        ch = bot._channel_settings.get(gid)
        if not ch:
            await query.answer("\u26a0\ufe0f 群组配置不存在。", show_alert=True)
            return

        # Parse: tpl_itv_inc_5 or tpl_itv_dec_1
        parts = query.data.split("_")  # ['tpl', 'itv', 'inc'/'dec', '1'/'5']
        action = parts[2]
        step = int(parts[3])

        current = ch.reply_interval
        if action == "inc":
            new_interval = current + step
        else:
            new_interval = max(1, current - step)

        ch.reply_interval = new_interval

        # Persist to groups_meta.json
        meta = bot._load_groups_meta()
        if str(gid) in meta:
            meta[str(gid)]["reply_interval"] = new_interval
            bot._save_groups_meta(meta)

        await query.answer(f"间隔已设为每 {new_interval} 条")

        text = f"\U0001f4ca 评论间隔设置\n\n当前间隔：每 {new_interval} 条帖子评论一次\n设为 1 表示每条都评论。"
        await query.edit_message_text(text, reply_markup=_build_interval_keyboard(new_interval))

    async def cb_preview_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_preview callback — show numbered buttons to pick a template to preview."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return

        templates = bot._templates.get(gid, [])
        if not templates:
            await query.edit_message_text("没有可预览的模板。")
            return

        text = _build_template_list_text(templates) + "\n\n请选择要预览的模板编号："
        await query.edit_message_text(text, reply_markup=_build_select_keyboard(templates, "tpl_pv"))

    async def cb_preview_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle tpl_pv_N callback — send template #N as a preview message."""
        query = update.callback_query
        await query.answer()

        gid = _get_group_id(context)
        if not gid:
            await query.edit_message_text("\u26a0\ufe0f 请先使用 /templates 选择群组。")
            return

        idx = int(query.data.split("_")[-1])
        templates = bot._templates.get(gid, [])

        if idx < 0 or idx >= len(templates):
            await query.edit_message_text("\u26a0\ufe0f 无效的模板编号。")
            return

        template = templates[idx]
        escaped_text = escape_markdown(template.text, version=2)

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

        # Send as a new message so the user sees the actual rendering
        prefix = escape_markdown(f"\U0001f50d 模板 #{idx + 1} 预览：\n\n", version=2)
        chat_id = update.effective_chat.id
        caption_or_text = f"{prefix}{escaped_text}"

        if template.media_file_id and template.media_type:
            send_func = {
                "photo": context.bot.send_photo,
                "animation": context.bot.send_animation,
                "video": context.bot.send_video,
            }.get(template.media_type)
            if send_func:
                await send_func(
                    chat_id=chat_id,
                    **{("photo" if template.media_type == "photo" else template.media_type): template.media_file_id},
                    caption=caption_or_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup,
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id, text=caption_or_text,
                    parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup,
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=caption_or_text,
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup,
            )

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
            await query.edit_message_text(text, reply_markup=_build_main_keyboard(len(templates)))
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
                MessageHandler(
                    (filters.PHOTO | filters.ANIMATION | filters.VIDEO) & ~filters.COMMAND,
                    handle_media_input,
                ),
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
    app.add_handler(CallbackQueryHandler(cb_freeze_select, pattern=r"^tpl_freeze$"))
    app.add_handler(CallbackQueryHandler(cb_freeze_toggle, pattern=r"^tpl_frz_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_delay_select, pattern=r"^tpl_delay$"))
    app.add_handler(CallbackQueryHandler(cb_delay_adjust, pattern=r"^tpl_dly_(inc|dec)_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_interval_select, pattern=r"^tpl_interval$"))
    app.add_handler(CallbackQueryHandler(cb_interval_adjust, pattern=r"^tpl_itv_(inc|dec)_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_preview_select, pattern=r"^tpl_preview$"))
    app.add_handler(CallbackQueryHandler(cb_preview_send, pattern=r"^tpl_pv_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_back, pattern=r"^tpl_back$"))

    logger.info("Template editor handlers registered")
