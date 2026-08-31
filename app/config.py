from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    channel_id: str
    webapp_url: str
    listen_host: str
    listen_port: int
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    order_email_to: str
    deadline_hour: int
    deadline_minute: int
    deadline_enabled: bool
    post_hour: int
    post_minute: int
    timezone_name: str
    telegram_proxy: str
    data_dir: Path

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


def load_settings() -> Settings:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip() or smtp_user
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        channel_id=os.getenv("CHANNEL_ID", "").strip(),
        webapp_url=os.getenv("WEBAPP_URL", "").strip().rstrip("/"),
        listen_host=os.getenv("LISTEN_HOST", "127.0.0.1"),
        listen_port=_int("LISTEN_PORT", 8080),
        smtp_host=os.getenv("SMTP_HOST", "smtp.yandex.ru").strip(),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=smtp_user,
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        smtp_from=smtp_from,
        order_email_to=os.getenv("ORDER_EMAIL_TO", "mail@edatomsk.ru").strip(),
        deadline_hour=_int("DEADLINE_HOUR", 9),
        deadline_minute=_int("DEADLINE_MINUTE", 15),
        deadline_enabled=_bool("DEADLINE_ENABLED", False),
        post_hour=_int("POST_HOUR", 8),
        post_minute=_int("POST_MINUTE", 0),
        timezone_name=os.getenv("TIMEZONE", "Asia/Tomsk").strip(),
        telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip(),
        data_dir=data_dir,
    )
