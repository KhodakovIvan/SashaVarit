from __future__ import annotations

import asyncio
import html
import logging
from datetime import date
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
    User,
    WebAppInfo,
)

from app.access import (
    NOT_IN_CHANNEL,
    channel_admin_ids,
    is_channel_admin,
    is_channel_member,
)
from app.ctx import Ctx
from app.domain import (
    email_body,
    format_summary,
    format_unavailable_report,
    is_weekday,
    person_total,
    today_in_tz,
    unavailable_in_orders,
)
from app.edatomsk import build_filled_xls, site_date_key
from app.mailer import send_order_email, smtp_configured
from app.phone import format_phone, normalize_phone
from app.telegram_auth import display_name

log = logging.getLogger(__name__)
router = Router()


class UnavailableItemsError(Exception):
    """Заказанные блюда больше недоступны на сайте."""


async def can_manage(bot: Bot, ctx: Ctx, user: User | None) -> bool:
    if not user or user.is_bot or not ctx.settings.channel_id:
        return False
    return await is_channel_admin(bot, ctx.settings.channel_id, user.id)


async def manager_user_ids(bot: Bot, ctx: Ctx) -> list[int]:
    if not ctx.settings.channel_id:
        return []
    return await channel_admin_ids(bot, ctx.settings.channel_id)


class CanManage(Filter):
    async def __call__(self, event: Message | CallbackQuery, ctx: Ctx, bot: Bot) -> bool:
        return await can_manage(bot, ctx, event.from_user)


class ChannelMemberMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        ctx: Ctx = data["ctx"]
        bot: Bot = data["bot"]
        user = getattr(event, "from_user", None)
        if user and not user.is_bot and ctx.settings.channel_id:
            if not await is_channel_member(bot, ctx.settings.channel_id, user.id):
                if isinstance(event, CallbackQuery):
                    await event.answer(NOT_IN_CHANNEL, show_alert=True)
                elif isinstance(event, Message) and event.chat.type == "private":
                    await event.answer(NOT_IN_CHANNEL)
                return None
        return await handler(event, data)


def menu_webapp_url(public_url: str) -> str:
    return public_url.rstrip("/") + "/"


def menu_reply_keyboard(public_url: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть меню", web_app=WebAppInfo(url=menu_webapp_url(public_url)))]],
        resize_keyboard=True,
        is_persistent=True,
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def user_label(user: User | None) -> str:
    if not user:
        return ""
    return display_name(
        {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "username": user.username,
        }
    )


def restore_menu_keyboard(ctx: Ctx) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if ctx.public_url:
        return menu_reply_keyboard(ctx.public_url)
    return ReplyKeyboardRemove()


def menu_inline_keyboard(public_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть меню", web_app=WebAppInfo(url=menu_webapp_url(public_url)))]
        ]
    )


async def setup_bot_menu_button(bot: Bot, public_url: str) -> None:
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Меню",
                web_app=WebAppInfo(url=menu_webapp_url(public_url)),
            )
        )
    except Exception as exc:
        log.warning("Кнопка меню у поля ввода не поставилась: %s", exc)


async def channel_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    me = await bot.get_me()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Заказать обед", url=f"https://t.me/{me.username}?start=order")]
        ]
    )


def send_keyboard(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить письмо", callback_data=f"send:{day.isoformat()}"),
                InlineKeyboardButton(text="Пока нет", callback_data="send:cancel"),
            ]
        ]
    )


async def post_menu(bot: Bot, ctx: Ctx, day: date | None = None) -> None:
    if not ctx.settings.channel_id:
        raise RuntimeError("CHANNEL_ID не задан")
    if not ctx.public_url:
        raise RuntimeError("Нет публичного HTTPS URL мини-приложения")
    day = day or today_in_tz(ctx.settings)
    menu = await ctx.cache.get(day)
    me = await bot.get_me()
    order_url = f"https://t.me/{me.username}?start=order"
    lines = [html.escape(menu.title)]
    if ctx.settings.deadline_enabled:
        deadline = f"{ctx.settings.deadline_hour:02d}:{ctx.settings.deadline_minute:02d}"
        lines.append(f"Заказы принимаются до {deadline}.")
    lines.append("")
    lines.append(
        f'<a href="{order_url}">Заказать обед</a> — откроется чат с ботом, там кнопка меню.'
    )
    text = "\n".join(lines)
    markup = await channel_keyboard(bot)
    await bot.send_message(
        chat_id=ctx.settings.channel_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    try:
        chat = await bot.get_chat(ctx.settings.channel_id)
        linked = getattr(chat, "linked_chat_id", None)
        if linked:
            await bot.send_message(
                chat_id=linked,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
    except Exception:
        log.info("Не удалось продублировать пост в обсуждение канала")
    await ctx.storage.set_closed(day, False)
    await ctx.storage.set_sent(day, False)


async def build_day_package(ctx: Ctx, day: date, *, refresh: bool = False) -> tuple[str, bytes, str, list]:
    menu = await ctx.cache.get(day, force=refresh)
    orders = await ctx.storage.list_orders(day)
    people = [(name, items) for _, name, items in orders]
    xls = build_filled_xls(menu, people)
    summary = format_summary(menu, orders)
    filename = f"zakaz_{site_date_key(day.year, day.month, day.day).replace('.', '-')}.xls"
    return summary, xls, filename, orders


@router.message(Command("start"))
async def cmd_start(message: Message, ctx: Ctx) -> None:
    uid = message.from_user.id
    extra = f"\n\nВаш Telegram id: {uid}"
    if await can_manage(message.bot, ctx, message.from_user):
        extra += (
            "\n\nКоманды администратора канала:\n"
            "/post — опубликовать меню в канал\n"
            "/summary — сводка за сегодня\n"
            "/close — закрыть сбор\n"
            "/open — открыть сбор снова\n"
            "/send — заполнить xls и отправить письмо\n"
            "/phone — контактный номер для письма на кухню"
        )
    if ctx.public_url:
        await message.answer(
            "Заказ обеда с edatomsk.ru.\n"
            "Кнопка «Открыть меню» — под этим сообщением." + extra,
            reply_markup=menu_inline_keyboard(ctx.public_url),
        )
        await message.answer(
            "Если под сообщением пусто, меню ещё внизу экрана, над полем ввода.",
            reply_markup=menu_reply_keyboard(ctx.public_url),
        )
    else:
        await message.answer("Мини-приложение ещё не поднято (нет HTTPS)." + extra)


@router.message(Command("menu"))
async def cmd_menu(message: Message, ctx: Ctx) -> None:
    if not ctx.public_url:
        await message.answer("Мини-приложение ещё не поднято.")
        return
    await message.answer(
        "Открыть меню:",
        reply_markup=menu_inline_keyboard(ctx.public_url),
    )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram id: {message.from_user.id}")


@router.message(Command("phone"), CanManage())
async def cmd_phone(message: Message, ctx: Ctx, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if raw:
        phone = normalize_phone(raw)
        if not phone:
            await message.answer("Не похоже на номер. Пример: /phone +79131234567")
            return
        await ctx.storage.set_admin_phone(message.from_user.id, phone)
        await message.answer(
            f"Номер сохранён: {format_phone(phone)}\n"
            "В письме на кухню уйдёт он, если заказ отправите вы.",
            reply_markup=restore_menu_keyboard(ctx),
        )
        return
    current = await ctx.storage.get_admin_phone(message.from_user.id)
    now = f"Сейчас: {format_phone(current)}" if current else "Номер ещё не задан."
    await message.answer(
        f"{now}\n\nНажмите кнопку ниже или напишите /phone +79131234567",
        reply_markup=phone_keyboard(),
    )


@router.message(F.contact)
async def on_contact(message: Message, ctx: Ctx) -> None:
    if not await can_manage(message.bot, ctx, message.from_user):
        return
    contact = message.contact
    if contact is None:
        return
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer("Пришлите свой номер, не чужой.")
        return
    phone = normalize_phone(contact.phone_number or "")
    if not phone:
        await message.answer("Не удалось разобрать номер. Напишите /phone +79131234567")
        return
    await ctx.storage.set_admin_phone(message.from_user.id, phone)
    await message.answer(
        f"Номер сохранён: {format_phone(phone)}\n"
        "В письме на кухню уйдёт он, если заказ отправите вы.",
        reply_markup=restore_menu_keyboard(ctx),
    )


@router.message(Command("post"), CanManage())
async def cmd_post(message: Message, ctx: Ctx) -> None:
    try:
        await post_menu(message.bot, ctx)
        await message.answer(
            "Меню опубликовано, сбор заказов открыт.\n"
            "Смотрите сам канал (не только комментарии). "
            "Кнопка «Заказать обед» ведёт в этот чат с ботом."
        )
    except Exception as exc:
        await message.answer(f"Не удалось опубликовать: {exc}")


@router.message(Command("summary"), CanManage())
async def cmd_summary(message: Message, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    try:
        summary, xls, filename, orders = await build_day_package(ctx, day, refresh=True)
    except Exception as exc:
        await message.answer(f"Не удалось получить меню с сайта: {exc}")
        return
    await message.answer_document(
        BufferedInputFile(xls, filename=filename),
        caption=summary[:1000] or "Пусто",
    )
    if not ctx.cache.meta_ok:
        await message.answer("Не удалось проверить доступность блюд на сайте.")
        return
    bad = unavailable_in_orders(await ctx.cache.get(day), orders)
    if bad:
        await message.answer(format_unavailable_report(bad, sending=False))


@router.message(Command("close"), CanManage())
async def cmd_close(message: Message, ctx: Ctx) -> None:
    await ctx.storage.set_closed(today_in_tz(ctx.settings), True)
    await message.answer("Сбор заказов закрыт.")


@router.message(Command("open"), CanManage())
async def cmd_open(message: Message, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    await ctx.storage.set_closed(day, False)
    await ctx.storage.set_sent(day, False)
    await message.answer("Сбор заказов открыт.")


@router.message(Command("send"), CanManage())
async def cmd_send(message: Message, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    if await ctx.storage.is_sent(day):
        await message.answer("Письмо за сегодня уже отправляли.")
        return
    try:
        summary, _, _, orders = await build_day_package(ctx, day, refresh=True)
    except Exception as exc:
        await message.answer(f"Не удалось получить меню с сайта: {exc}")
        return
    if not ctx.cache.meta_ok:
        await message.answer("Не удалось получить актуальное меню с сайта. Письмо не отправлял, сбор открыт.")
        return
    if not orders:
        await message.answer("Некого отправлять: заказов нет.")
        return
    if not ctx.settings.delivery_address:
        await message.answer("В .env нет DELIVERY_ADDRESS. Без адреса доставки письмо не отправлю.")
        return
    if not await ctx.storage.get_admin_phone(message.from_user.id):
        await message.answer(
            "Для письма нужен ваш контактный номер. Нажмите кнопку или напишите /phone +79131234567",
            reply_markup=phone_keyboard(),
        )
        return
    bad = unavailable_in_orders(await ctx.cache.get(day), orders)
    if bad:
        await message.answer(format_unavailable_report(bad))
        return
    await message.answer(
        "Отправить лист заказа на mail@edatomsk.ru?\n\n" + summary[:3500],
        reply_markup=send_keyboard(day),
    )


@router.callback_query(F.data == "send:cancel")
async def cb_cancel(cb: CallbackQuery, ctx: Ctx) -> None:
    if not await can_manage(cb.bot, ctx, cb.from_user):
        await cb.answer()
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Отменено")


@router.callback_query(F.data.startswith("send:"))
async def cb_send(cb: CallbackQuery, ctx: Ctx) -> None:
    if not await can_manage(cb.bot, ctx, cb.from_user):
        await cb.answer()
        return
    day = date.fromisoformat(cb.data.split(":", 1)[1])
    if await ctx.storage.is_sent(day):
        await cb.answer("Уже отправлено")
        return
    if not ctx.settings.delivery_address:
        await cb.message.answer("В .env нет DELIVERY_ADDRESS. Без адреса доставки письмо не отправлю.")
        await cb.answer("Нет адреса", show_alert=True)
        return
    if not await ctx.storage.get_admin_phone(cb.from_user.id):
        await cb.message.answer(
            "Для письма нужен ваш контактный номер. Нажмите кнопку или напишите /phone +79131234567",
            reply_markup=phone_keyboard(),
        )
        await cb.answer("Нет телефона", show_alert=True)
        return
    try:
        dry_run = await actually_send(cb.bot, ctx, day, sender=cb.from_user)
        await cb.message.edit_reply_markup(reply_markup=None)
        if dry_run:
            await cb.message.answer(
                "SMTP не задан (SMTP_USER / SMTP_PASSWORD). "
                "Письмо на mail@edatomsk.ru не уходило.\n"
                "Для теста заказ помечен как отправленный: сбор закрыт, в канал ушёл xls."
            )
            await cb.answer("Почта не настроена, тест", show_alert=True)
        else:
            await cb.message.answer("Письмо отправлено.")
            await cb.answer("Отправлено")
    except UnavailableItemsError as exc:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.message.answer(str(exc))
        await cb.answer("Есть недоступные блюда", show_alert=True)
    except Exception as exc:
        log.exception("send failed")
        await cb.message.answer(f"Не удалось отправить: {exc}")
        await cb.answer("Ошибка", show_alert=True)


async def actually_send(bot: Bot, ctx: Ctx, day: date, sender: User) -> bool:
    menu = await ctx.cache.get(day, force=True)
    if not ctx.cache.meta_ok:
        raise RuntimeError("Не удалось получить актуальное меню с сайта")
    orders = await ctx.storage.list_orders(day)
    if not orders:
        raise RuntimeError("Нет заказов")
    address = ctx.settings.delivery_address
    if not address:
        raise RuntimeError("В .env не задан DELIVERY_ADDRESS")
    phone = await ctx.storage.get_admin_phone(sender.id)
    if not phone:
        raise RuntimeError("Сначала сохраните номер: /phone")
    bad = unavailable_in_orders(menu, orders)
    if bad:
        raise UnavailableItemsError(format_unavailable_report(bad))
    people = [(name, items) for _, name, items in orders]
    xls = build_filled_xls(menu, people)
    filename = f"zakaz_{site_date_key(day.year, day.month, day.day).replace('.', '-')}.xls"
    grand = sum(person_total(menu, items) for _, _, items in orders)
    subject = (
        f"Заказ обедов {site_date_key(day.year, day.month, day.day)} / "
        f"{len(orders)} персон / {int(grand)} руб"
    )
    body = email_body(
        menu,
        orders,
        address=address,
        address_comment=ctx.settings.delivery_comment,
        contact_name=user_label(sender),
        contact_phone=format_phone(phone),
    )
    dry_run = not smtp_configured(ctx.settings)
    if dry_run:
        log.warning("SMTP не задан, письмо не отправляем, имитация отправки")
        await bot.send_document(
            sender.id,
            BufferedInputFile(xls, filename=filename),
            caption="Тест: SMTP пустой, на почту не отправлялось. Лист заказа во вложении.",
        )
        await bot.send_message(sender.id, "Текст письма:\n\n" + body[:3500])
    else:
        await asyncio.to_thread(
            send_order_email,
            ctx.settings,
            subject,
            body,
            xls,
            filename,
        )
    await ctx.storage.set_sent(day, True)
    await ctx.storage.set_closed(day, True)
    await ctx.storage.clear_orders(day)
    if ctx.settings.channel_id:
        caption = "Обед заказан!"
        if dry_run:
            caption += "\nТест: письмо на кухню не отправлялось."
        await bot.send_document(
            ctx.settings.channel_id,
            BufferedInputFile(xls, filename=filename),
            caption=caption,
        )
    return dry_run


async def notify_deadline(bot: Bot, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    if not is_weekday(day):
        return
    await ctx.storage.set_closed(day, True)
    recipients = await manager_user_ids(bot, ctx)
    if not recipients:
        return
    summary, _, _, orders = await build_day_package(ctx, day)
    text = "Дедлайн. Сбор закрыт.\n\n" + summary
    markup = send_keyboard(day) if orders else None
    for uid in recipients:
        try:
            await bot.send_message(uid, text[:4000], reply_markup=markup)
        except (TelegramForbiddenError, TelegramBadRequest):
            log.info("Не удалось написать администратору %s", uid)


async def scheduled_post(bot: Bot, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    if not is_weekday(day):
        return
    await post_menu(bot, ctx, day)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    router.message.middleware(ChannelMemberMiddleware())
    router.callback_query.middleware(ChannelMemberMiddleware())
    dp.include_router(router)
    return dp
