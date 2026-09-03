from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path

from aiohttp import web

from app.access import NOT_IN_CHANNEL, is_channel_member
from app.config import Settings
from app.domain import dish_label, format_user_receipt, is_after_deadline, person_total, today_in_tz
from app.edatomsk import (
    DayMenu,
    apply_details,
    apply_item_meta,
    download_items_meta,
    download_menu_page,
    download_xls,
    menu_to_json,
    parse_menu,
    parse_menu_page,
    site_date_key,
)
from app.storage import Storage
from app.telegram_auth import display_name, validate_init_data

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "webapp"


class MenuCache:
    def __init__(self, storage: Storage | None = None) -> None:
        self._menu: DayMenu | None = None
        self._lock = asyncio.Lock()
        self._storage = storage
        self.meta_ok = False

    async def _remember_names(self, menu: DayMenu) -> None:
        if self._storage is None:
            return
        try:
            await self._storage.upsert_dish_names(menu.names_by_id)
            known = await self._storage.get_dish_names()
            for dish_id, name in known.items():
                menu.names_by_id.setdefault(dish_id, name)
        except Exception:
            log.exception("Не удалось сохранить названия блюд")

    async def get(self, day: date, *, force: bool = False) -> DayMenu:
        key = site_date_key(day.year, day.month, day.day)
        async with self._lock:
            if not force and self._menu and self._menu.date_key == key:
                return self._menu
            raw = await download_xls(key, 1)
            self._menu = parse_menu(raw, key)
            self.meta_ok = False
            try:
                meta = await download_items_meta(key)
                apply_item_meta(self._menu, meta)
                self.meta_ok = True
            except Exception:
                log.exception("Не удалось обновить признаки блюд с сайта")
            try:
                page = await download_menu_page(key)
                apply_details(self._menu, parse_menu_page(page))
            except Exception:
                log.exception("Не удалось подтянуть карточки блюд с сайта")
            await self._remember_names(self._menu)
            return self._menu

    def peek(self) -> DayMenu | None:
        return self._menu


async def _require_channel_member(request: web.Request) -> tuple[dict | None, web.Response | None]:
    settings: Settings = request.app["settings"]
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query.get("initData") or ""
    try:
        user = validate_init_data(init_data, settings.bot_token)
    except ValueError as exc:
        return None, web.json_response({"ok": False, "error": str(exc)}, status=401)
    bot = request.app.get("bot")
    channel_id = settings.channel_id
    if bot is None or not channel_id:
        return None, web.json_response({"ok": False, "error": "Бот ещё не готов"}, status=503)
    if not await is_channel_member(bot, channel_id, int(user["id"])):
        return None, web.json_response({"ok": False, "error": NOT_IN_CHANNEL}, status=403)
    return user, None


def create_http_app(settings: Settings, storage: Storage, cache: MenuCache) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["storage"] = storage
    app["cache"] = cache

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "index.html")

    async def api_menu(request: web.Request) -> web.Response:
        user, err = await _require_channel_member(request)
        if err is not None:
            return err
        settings = request.app["settings"]
        storage = request.app["storage"]
        day = today_in_tz(settings)
        menu = await request.app["cache"].get(day)
        closed = await storage.is_closed(day) or is_after_deadline(settings, day)
        payload = menu_to_json(menu)
        payload["closed"] = closed
        payload["deadline"] = (
            f"{settings.deadline_hour:02d}:{settings.deadline_minute:02d}"
            if settings.deadline_enabled
            else ""
        )
        items = await storage.get_order(day, int(user["id"]))
        try:
            await storage.upsert_roster(int(user["id"]), display_name(user))
        except Exception:
            log.exception("Не удалось запомнить пользователя Mini App")
        payload["my"] = {str(k): v for k, v in items.items()}
        missing = [
            {
                "id": dish_id,
                "name": dish_label(menu, dish_id),
                "price": 0,
                "weighty": False,
                "available": False,
            }
            for dish_id, qty in items.items()
            if qty and dish_id not in menu.dishes_by_id
        ]
        if missing:
            payload["categories"].append({"name": "Нет на сайте", "dishes": missing})
        return web.json_response(payload)

    async def api_order(request: web.Request) -> web.Response:
        user, err = await _require_channel_member(request)
        if err is not None:
            return err
        settings = request.app["settings"]
        storage = request.app["storage"]
        day = today_in_tz(settings)
        if await storage.is_closed(day) or is_after_deadline(settings, day):
            return web.json_response({"ok": False, "error": "Сбор заказов закрыт"}, status=403)
        if await storage.is_sent(day):
            return web.json_response({"ok": False, "error": "Заказ уже отправлен"}, status=403)

        body = await request.json()
        raw_items = body.get("items") or {}
        items: dict[int, int] = {}
        menu = await request.app["cache"].get(day)
        for key, qty in raw_items.items():
            dish_id = int(key)
            count = int(qty)
            if dish_id in menu.dishes_by_id and count > 0:
                items[dish_id] = min(count, 99)
        name = display_name(user)
        user_id = int(user["id"])
        try:
            await storage.upsert_roster(user_id, name)
        except Exception:
            log.exception("Не удалось запомнить пользователя Mini App")
        if items:
            await storage.upsert_order(day, user_id, name, items)
        else:
            await storage.delete_order(day, user_id)
        amount = person_total(menu, items)
        summary = format_user_receipt(menu, items)
        bot = request.app.get("bot")
        if bot is not None:
            try:
                await bot.send_message(user_id, summary)
            except Exception:
                log.exception("Не удалось написать пользователю после сохранения")
        return web.json_response(
            {
                "ok": True,
                "total": amount,
                "summary": summary,
                "items": {str(k): v for k, v in items.items()},
            }
        )

    app.router.add_get("/", index)
    app.router.add_get("/api/menu", api_menu)
    app.router.add_post("/api/order", api_order)
    app.router.add_static("/static", WEB_DIR)
    return app


async def start_http(app: web.Application, settings: Settings) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.listen_host, settings.listen_port)
    await site.start()
    log.info("HTTP %s:%s", settings.listen_host, settings.listen_port)
    return runner
