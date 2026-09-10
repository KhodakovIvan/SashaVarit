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
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    delivery_address: str
    delivery_comment: str
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
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    imap_user = os.getenv("IMAP_USER", "").strip() or smtp_user
    imap_password = os.getenv("IMAP_PASSWORD", "").strip()
    # Один пароль приложения часто подходит и для SMTP, и для IMAP
    if not smtp_password and imap_password:
        smtp_password = imap_password
    if not imap_password and smtp_password:
        imap_password = smtp_password
    smtp_host = os.getenv("SMTP_HOST", "smtp.mail.ru").strip()
    imap_host = os.getenv("IMAP_HOST", "").strip()
    if not imap_host and "mail.ru" in smtp_host.lower():
        imap_host = "imap.mail.ru"
    elif not imap_host and "yandex" in smtp_host.lower():
        imap_host = "imap.yandex.ru"
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        channel_id=os.getenv("CHANNEL_ID", "").strip(),
        webapp_url=os.getenv("WEBAPP_URL", "").strip().rstrip("/"),
        listen_host=os.getenv("LISTEN_HOST", "127.0.0.1"),
        listen_port=_int("LISTEN_PORT", 8080),
        smtp_host=smtp_host,
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        smtp_from=smtp_from,
        order_email_to=os.getenv("ORDER_EMAIL_TO", "mail@edatomsk.ru").strip(),
        imap_host=imap_host,
        imap_port=_int("IMAP_PORT", 993),
        imap_user=imap_user,
        imap_password=imap_password,
        delivery_address=os.getenv("DELIVERY_ADDRESS", "").strip(),
        delivery_comment=os.getenv("DELIVERY_COMMENT", "").strip(),
        deadline_hour=_int("DEADLINE_HOUR", 9),
        deadline_minute=_int("DEADLINE_MINUTE", 15),
        deadline_enabled=_bool("DEADLINE_ENABLED", False),
        post_hour=_int("POST_HOUR", 8),
        post_minute=_int("POST_MINUTE", 0),
        timezone_name=os.getenv("TIMEZONE", "Asia/Tomsk").strip(),
        telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip(),
        data_dir=data_dir,
    )
