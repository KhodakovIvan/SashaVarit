from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot import create_dispatcher, notify_deadline, scheduled_post, setup_bot_menu_button
from app.config import load_settings
from app.ctx import Ctx
from app.storage import Storage
from app.tunnel import start_cloudflared
from app.webapp_server import MenuCache, create_http_app, start_http

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sashavarit")


async def main() -> None:
    settings = load_settings()
    if not settings.bot_token:
        raise SystemExit("Заполните BOT_TOKEN в файле .env (скопируйте из .env.example)")

    storage = Storage(settings.data_dir / "orders.sqlite")
    await storage.init()
    cache = MenuCache(storage)
    ctx = Ctx(settings=settings, storage=storage, cache=cache, public_url=settings.webapp_url)

    http_app = create_http_app(settings, storage, cache)
    runner = await start_http(http_app, settings)

    tunnel_proc = None
    if not ctx.public_url:
        tunnel_proc, url = await start_cloudflared(settings.listen_port)
        if url:
            ctx.public_url = url
            log.info("Tunnel URL: %s", url)
        else:
            log.warning(
                "HTTPS для мини-приложения не получен. "
                "Установите cloudflared или задайте WEBAPP_URL в .env"
            )

    session = None
    if settings.telegram_proxy:
        log.info("Telegram API через прокси %s", settings.telegram_proxy)
        session = AiohttpSession(proxy=settings.telegram_proxy)
    bot = Bot(settings.bot_token, session=session)
    http_app["bot"] = bot
    dp = create_dispatcher()
    dp.workflow_data.update(ctx=ctx)

    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(
        scheduled_post,
        "cron",
        day_of_week="mon-fri",
        hour=settings.post_hour,
        minute=settings.post_minute,
        kwargs={"bot": bot, "ctx": ctx},
        id="post_menu",
    )
    if settings.deadline_enabled:
        scheduler.add_job(
            notify_deadline,
            "cron",
            day_of_week="mon-fri",
            hour=settings.deadline_hour,
            minute=settings.deadline_minute,
            kwargs={"bot": bot, "ctx": ctx},
            id="deadline",
        )
    scheduler.start()

    log.info("Бот запущен. Публичный URL: %s", ctx.public_url or "(нет)")
    if ctx.public_url:
        await setup_bot_menu_button(bot, ctx.public_url)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
        )
    except Exception as exc:
        msg = str(exc)
        if "Couldn't connect to proxy" in msg or "ProxyConnectionError" in type(exc).__name__:
            raise SystemExit(
                f"Нет соединения с прокси {settings.telegram_proxy}. "
                "Включите Hiddify/v2rayN и проверьте порт SOCKS."
            ) from exc
        if "api.telegram.org" in msg:
            raise SystemExit(
                "Нет доступа к api.telegram.org. "
                "Нужен рабочий локальный прокси в TELEGRAM_PROXY."
            ) from exc
        raise
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await runner.cleanup()
        if tunnel_proc and tunnel_proc.returncode is None:
            tunnel_proc.terminate()
            try:
                await asyncio.wait_for(tunnel_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                tunnel_proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
