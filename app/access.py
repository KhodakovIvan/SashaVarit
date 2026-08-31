from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

log = logging.getLogger(__name__)

NOT_IN_CHANNEL = "Бот работает только для подписчиков канала."

_TTL_SEC = 60.0
_status_cache: dict[tuple[str, int], tuple[float, ChatMemberStatus | None]] = {}
_admin_ids_cache: dict[str, tuple[float, list[int]]] = {}

_IN_CHANNEL = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
}
_ADMIN = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def member_status(bot: Bot, channel_id: str, user_id: int) -> ChatMemberStatus | None:
    key = (str(channel_id), user_id)
    now = time.monotonic()
    cached = _status_cache.get(key)
    if cached and now - cached[0] < _TTL_SEC:
        return cached[1]
    status: ChatMemberStatus | None
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        status = ChatMemberStatus(member.status)
    except TelegramBadRequest:
        status = None
    except Exception:
        log.warning("Не удалось проверить участника %s канала %s", user_id, channel_id)
        status = None
    _status_cache[key] = (now, status)
    return status


async def is_channel_member(bot: Bot, channel_id: str, user_id: int) -> bool:
    status = await member_status(bot, channel_id, user_id)
    return status in _IN_CHANNEL


async def is_channel_admin(bot: Bot, channel_id: str, user_id: int) -> bool:
    status = await member_status(bot, channel_id, user_id)
    return status in _ADMIN


async def channel_admin_ids(bot: Bot, channel_id: str) -> list[int]:
    now = time.monotonic()
    cached = _admin_ids_cache.get(channel_id)
    if cached and now - cached[0] < _TTL_SEC:
        return list(cached[1])
    try:
        members = await bot.get_chat_administrators(channel_id)
        found = [m.user.id for m in members if m.user and not m.user.is_bot]
    except Exception:
        log.warning("Не удалось получить список администраторов канала %s", channel_id)
        found = []
    _admin_ids_cache[channel_id] = (now, found)
    return list(found)
