from __future__ import annotations

from aiogram.utils.web_app import safe_parse_webapp_init_data


def validate_init_data(init_data: str, bot_token: str) -> dict:
    if not init_data:
        raise ValueError("Нет initData")
    parsed = safe_parse_webapp_init_data(token=bot_token, init_data=init_data)
    user = parsed.user
    if user is None:
        raise ValueError("В initData нет user")
    return {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username,
    }


def display_name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    username = (user.get("username") or "").strip()
    full = " ".join(part for part in (first, last) if part)
    if full:
        return full
    if username:
        return username
    return str(user.get("id"))
