import json
import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass
class Template:
    text: str
    weight: int = 1


@dataclass
class Settings:
    default_reply_delay_seconds: int = 3
    default_template_file: str = "default.json"
    log_file: str = "bot.log"
    data_dir: str = "data"
    owner_id: int = 0


@dataclass
class ChannelConfig:
    channel_id: int
    discussion_group_id: int
    template_file: str = "default.json"
    reply_delay_seconds: int = 3


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

        bots.append(BotConfig(
            name=bot_data["name"],
            token=token,
            channels=channels,
            bot_class=bot_data.get("bot_class", "ChannelReviewBot"),
            settings=settings,
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
        templates.append(Template(text=t["text"], weight=t.get("weight", 1)))

    if not templates:
        raise ValueError(f"No templates found in {path}")

    return templates


def select_template(templates: list[Template]) -> str:
    """Select a template text using weighted random choice."""
    texts = [t.text for t in templates]
    weights = [t.weight for t in templates]
    return random.choices(texts, weights=weights, k=1)[0]
