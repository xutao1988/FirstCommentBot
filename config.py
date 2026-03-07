import json
import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# MarkdownV2 special chars that are NOT formatting syntax
_MD_ESCAPE_CHARS = r'\.!>#+\-=|{}'


def escape_markdown_v2(text: str) -> str:
    """Escape non-formatting MarkdownV2 special characters.

    Preserves formatting syntax: * _ ~ ` [ ] ( )
    Escapes: . ! > # + - = | { }
    """
    result = []
    for ch in text:
        if ch in _MD_ESCAPE_CHARS:
            result.append(f'\\{ch}')
        else:
            result.append(ch)
    return ''.join(result)


@dataclass
class Template:
    text: str
    weight: int = 1
    buttons: list[list[dict]] = field(default_factory=list)  # rows of buttons
    frozen: bool = False
    media_file_id: str = ""   # Telegram file_id, empty = no media
    media_type: str = ""      # "photo" / "animation" / "video", empty = text-only


@dataclass
class Settings:
    default_reply_delay_seconds: int = 3
    default_template_file: str = "default.json"
    log_file: str = "bot.log"
    data_dir: str = "data"
    owner_id: int = 0
    stats_channel_id: int = 0  # 统计频道 ID，0=不发送


@dataclass
class ChannelConfig:
    channel_id: int
    discussion_group_id: int
    template_file: str = "default.json"
    reply_delay_seconds: int = 3
    reply_interval: int = 1  # 每 N 条帖子评论一次，1=每条都评


@dataclass
class BotConfig:
    name: str
    token: str
    channels: list[ChannelConfig] = field(default_factory=list)
    bot_class: str = "ChannelReviewBot"
    settings: Settings = field(default_factory=Settings)


@dataclass
class AppConfig:
    bots: list[BotConfig] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)


def _normalize_buttons(raw: list) -> list[list[dict]]:
    """Normalize buttons from JSON — support both old flat and new nested format.

    Old: [{"text": ..., "url": ...}, ...]        → wrap each in its own row
    New: [[{"text": ..., "url": ...}, ...], ...]  → use as-is
    """
    if not raw:
        return []
    if isinstance(raw[0], dict):
        # Old flat format: each button becomes its own row
        return [[btn] for btn in raw]
    return raw


def load_config(config_path: str = "config.json") -> AppConfig:
    """Load application config from JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    settings_data = data.get("settings", {})
    settings = Settings(
        default_reply_delay_seconds=settings_data.get("default_reply_delay_seconds", 3),
        default_template_file=settings_data.get("default_template_file", "default.json"),
        log_file=settings_data.get("log_file", "bot.log"),
        data_dir=settings_data.get("data_dir", "data"),
        owner_id=settings_data.get("owner_id", 0),
        stats_channel_id=settings_data.get("stats_channel_id", 0),
    )

    bots = []
    for bot_data in data.get("bots", []):
        token_env = bot_data["token_env"]
        token = os.getenv(token_env)
        if not token:
            logger.warning("Token env var %s not set, skipping bot %s", token_env, bot_data["name"])
            continue

        channels = []
        for ch in bot_data.get("channels", []):
            channels.append(ChannelConfig(
                channel_id=ch["channel_id"],
                discussion_group_id=ch["discussion_group_id"],
                template_file=ch.get("template_file", "default.json"),
                reply_delay_seconds=ch.get("reply_delay_seconds", settings.default_reply_delay_seconds),
            ))

        bot_tpl_file = bot_data.get("default_template_file")
        if bot_tpl_file:
            bot_settings = Settings(
                default_reply_delay_seconds=settings.default_reply_delay_seconds,
                default_template_file=bot_tpl_file,
                log_file=settings.log_file,
                data_dir=settings.data_dir,
                owner_id=settings.owner_id,
                stats_channel_id=settings.stats_channel_id,
            )
        else:
            bot_settings = settings

        bots.append(BotConfig(
            name=bot_data["name"],
            token=token,
            channels=channels,
            bot_class=bot_data.get("bot_class", "ChannelReviewBot"),
            settings=bot_settings,
        ))

    return AppConfig(bots=bots, settings=settings)


def load_templates(template_file: str) -> list[Template]:
    """Load comment templates from a JSON file in the templates directory."""
    path = TEMPLATES_DIR / template_file
    if not path.exists():
        fallback = TEMPLATES_DIR / "default.json"
        if not fallback.exists():
            raise FileNotFoundError(f"Neither {template_file} nor default.json found in templates/")
        logger.warning("Template %s not found, falling back to default.json", template_file)
        path = fallback

    with open(path, encoding="utf-8") as f:
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

    if not templates:
        raise ValueError(f"No templates found in {path}")

    return templates


def select_template(templates: list[Template]) -> Template | None:
    """Select a template using weighted random choice, skipping frozen ones."""
    active = [t for t in templates if not t.frozen]
    if not active:
        return None
    weights = [t.weight for t in active]
    return random.choices(active, weights=weights, k=1)[0]
