from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ParseMode
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
    ChatMemberUpdated,
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
    format_dropped_notice,
    format_summary,
    format_unavailable_report,
    is_after_deadline,
    is_weekday,
    person_total,
    strip_unavailable,
    today_in_tz,
    unavailable_in_orders,
)
from app.edatomsk import build_filled_xls, site_date_key
from app.mailer import send_order_email, send_text_email, smtp_configured, smtp_ready
from app.phone import format_phone, normalize_phone
from app.telegram_auth import display_name

log = logging.getLogger(__name__)
router = Router()

_pending_testmail: set[int] = set()
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UnavailableItemsError(Exception):
    """Заказанные блюда больше недоступны на сайте."""


class WaitingTestMail(Filter):
    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if not user or user.id not in _pending_testmail:
            return False
        text = (message.text or "").strip()
        return bool(text) and not text.startswith("/")

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
            try:
                await ctx.storage.upsert_roster(user.id, user_label(user))
            except Exception:
                log.exception("Не удалось запомнить пользователя %s", user.id)
        return await handler(event, data)


_IN_CHANNEL = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
}


def _same_chat(chat_id: int, channel_id: str) -> bool:
    return str(chat_id) == str(channel_id).strip()


@router.chat_member()
async def on_channel_member(event: ChatMemberUpdated, ctx: Ctx) -> None:
    if not ctx.settings.channel_id or not _same_chat(event.chat.id, ctx.settings.channel_id):
        return
    member = event.new_chat_member
    user = member.user
    if user is None or user.is_bot:
        return
    status = ChatMemberStatus(member.status)
    if status in _IN_CHANNEL:
        await ctx.storage.upsert_roster(user.id, user_label(user))
        return
    await ctx.storage.delete_roster(user.id)


def menu_webapp_url(public_url: str) -> str:
    return public_url.rstrip("/") + "/"


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


def menu_inline_keyboard(public_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть меню", web_app=WebAppInfo(url=menu_webapp_url(public_url)))],
            [InlineKeyboardButton(text="Пропускаю", callback_data="skip:today")],
        ]
    )


def _menu_button(public_url: str) -> MenuButtonWebApp:
    return MenuButtonWebApp(
        text="Меню",
        web_app=WebAppInfo(url=menu_webapp_url(public_url)),
    )


async def setup_bot_menu_button(bot: Bot, public_url: str, *, chat_id: int | None = None) -> None:
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=_menu_button(public_url))
    except Exception as exc:
        target = f"чата {chat_id}" if chat_id else "по умолчанию"
        log.warning("Кнопка меню у поля ввода (%s) не обновилась: %s", target, exc)


async def refresh_user_menu(bot: Bot, public_url: str, chat_id: int) -> None:
    await setup_bot_menu_button(bot, public_url, chat_id=chat_id)


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


def drop_keyboard(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Убрать снятые и продолжить",
                    callback_data=f"send:drop:{day.isoformat()}",
                )
            ],
            [InlineKeyboardButton(text="Пока нет", callback_data="send:cancel")],
        ]
    )


def summary_clear_keyboard(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Файл и очистить",
                    callback_data=f"clear:{day.isoformat()}",
                ),
                InlineKeyboardButton(text="Пока нет", callback_data="clear:cancel"),
            ]
        ]
    )


def split_telegram_text(text: str, limit: int = 4000) -> list[str]:
    text = (text or "").strip() or "Пусто"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks


def compose_summary_message(
    summary: str,
    *,
    lead: str = "",
    trail: str = "",
) -> tuple[str, str | None]:
    """Сводка: сворачиваемая цитата, если в теле ≥ 3 строк."""
    body = (summary or "").strip() or "Пусто"
    lead = (lead or "").strip()
    trail = (trail or "").strip()
    use_quote = len(body.splitlines()) >= 3
    if use_quote:
        parts: list[str] = []
        if lead:
            parts.append(html.escape(lead))
        parts.append(f"<blockquote expandable>{html.escape(body)}</blockquote>")
        if trail:
            parts.append(html.escape(trail))
        return "\n\n".join(parts), ParseMode.HTML
    parts = [p for p in (lead, body, trail) if p]
    return "\n\n".join(parts), None


async def answer_long(target: Message, text: str, *, reply_markup=None) -> None:
    chunks = split_telegram_text(text)
    last = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        await target.answer(chunk, reply_markup=reply_markup if i == last else None)


async def answer_summary(
    target: Message,
    summary: str,
    *,
    lead: str = "",
    trail: str = "",
    reply_markup=None,
) -> None:
    body = (summary or "").strip() or "Пусто"
    overhead = len("<blockquote expandable></blockquote>") + len(lead) + len(trail) + 10
    limit = max(1500, 4000 - overhead)
    chunks = split_telegram_text(body, limit=limit)
    last = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        text, mode = compose_summary_message(
            chunk,
            lead=lead if i == 0 else "",
            trail=trail if i == last else "",
        )
        kwargs: dict[str, Any] = {"reply_markup": reply_markup if i == last else None}
        if mode:
            kwargs["parse_mode"] = mode
        await target.answer(text, **kwargs)


async def send_summary(
    bot: Bot,
    chat_id: int,
    summary: str,
    *,
    lead: str = "",
    trail: str = "",
    reply_markup=None,
) -> None:
    body = (summary or "").strip() or "Пусто"
    overhead = len("<blockquote expandable></blockquote>") + len(lead) + len(trail) + 10
    limit = max(1500, 4000 - overhead)
    chunks = split_telegram_text(body, limit=limit)
    last = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        text, mode = compose_summary_message(
            chunk,
            lead=lead if i == 0 else "",
            trail=trail if i == last else "",
        )
        kwargs: dict[str, Any] = {"reply_markup": reply_markup if i == last else None}
        if mode:
            kwargs["parse_mode"] = mode
        await bot.send_message(chat_id, text, **kwargs)


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
    xls = await build_filled_xls(menu, people)
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
            "/summary_clear — выгрузить xls и стереть заказы\n"
            "/status — кто заказал, пропустил или молчит\n"
            "/close — закрыть сбор\n"
            "/open — открыть сбор снова\n"
            "/send — заполнить xls и отправить письмо\n"
            "/testmail — проверить SMTP тестовым письмом\n"
            "/myphone — контактный номер для письма на кухню\n"
            "/mymail — почта админа (копия письма на кухню)"
        )
    if ctx.public_url:
        await refresh_user_menu(message.bot, ctx.public_url, message.chat.id)
        await message.answer(
            "Заказ обеда с edatomsk.ru.\n"
            "«Открыть меню» — под сообщением, «Меню» — слева от поля ввода.\n"
            "Если обед сегодня не нужен — нажмите «Пропускаю»." + extra,
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(
            "Выберите действие:",
            reply_markup=menu_inline_keyboard(ctx.public_url),
        )
    else:
        await message.answer(
            "Мини-приложение ещё не поднято (нет HTTPS)." + extra,
            reply_markup=ReplyKeyboardRemove(),
        )


@router.message(Command("menu"))
async def cmd_menu(message: Message, ctx: Ctx) -> None:
    if not ctx.public_url:
        await message.answer("Мини-приложение ещё не поднято.")
        return
    await refresh_user_menu(message.bot, ctx.public_url, message.chat.id)
    await message.answer(
        "Выберите действие:",
        reply_markup=menu_inline_keyboard(ctx.public_url),
    )


@router.callback_query(F.data == "skip:today")
async def cb_skip_today(cb: CallbackQuery, ctx: Ctx) -> None:
    if not cb.from_user or cb.from_user.is_bot:
        await cb.answer()
        return
    day = today_in_tz(ctx.settings)
    if await ctx.storage.is_sent(day):
        await cb.answer("Заказ на сегодня уже отправлен", show_alert=True)
        return
    if await ctx.storage.is_closed(day) or is_after_deadline(ctx.settings, day):
        await cb.answer("Сбор заказов закрыт", show_alert=True)
        return
    name = user_label(cb.from_user)
    await ctx.storage.set_skip(day, cb.from_user.id, name)
    try:
        await ctx.storage.upsert_roster(cb.from_user.id, name)
    except Exception:
        log.exception("Не удалось запомнить пользователя %s", cb.from_user.id)
    await cb.answer("Ок, обед пропускаете")
    await cb.message.answer(
        "Отметил: сегодня без обеда.\n"
        "Если передумаете — откройте меню и сохраните заказ."
    )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram id: {message.from_user.id}")


async def _send_test_mail(message: Message, ctx: Ctx, raw_to: str) -> None:
    to = raw_to.strip().lower()
    if not _EMAIL_RE.match(to):
        await message.answer("Не похоже на email. Пример: name@mail.ru")
        return
    if not smtp_ready(ctx.settings):
        await message.answer("SMTP не настроен: нужен SMTP_USER / SMTP_PASSWORD / SMTP_FROM в .env")
        return
    body = (
        "Тестовое письмо от бота SashaVarit.\n"
        f"От: {ctx.settings.smtp_from}\n"
        f"SMTP: {ctx.settings.smtp_host}:{ctx.settings.smtp_port}\n"
        "Если это письмо дошло — отправка настроена верно."
    )
    try:
        await asyncio.to_thread(
            send_text_email,
            ctx.settings,
            to=to,
            subject="SashaVarit: тест SMTP",
            body=body,
        )
    except Exception as exc:
        log.exception("testmail failed")
        await message.answer(f"Не удалось отправить: {exc}")
        return
    await message.answer(
        f"Тестовое письмо отправлено на {to}.\n"
        f"Отправитель: {ctx.settings.smtp_from}"
    )


@router.message(Command("testmail"), CanManage())
async def cmd_testmail(message: Message, ctx: Ctx, command: CommandObject) -> None:
    if not smtp_ready(ctx.settings):
        await message.answer("SMTP не настроен: нужен SMTP_USER / SMTP_PASSWORD / SMTP_FROM в .env")
        return
    raw = (command.args or "").strip()
    if raw:
        _pending_testmail.discard(message.from_user.id)
        await _send_test_mail(message, ctx, raw)
        return
    _pending_testmail.add(message.from_user.id)
    await message.answer(
        f"Отправитель: {ctx.settings.smtp_from}\n"
        f"SMTP: {ctx.settings.smtp_host}:{ctx.settings.smtp_port}\n\n"
        "Напишите email получателя тестового письма.\n"
        "Или сразу: /testmail name@mail.ru\n"
        "Отмена: /cancel"
    )


@router.message(Command("cancel"), CanManage())
async def cmd_cancel(message: Message) -> None:
    if message.from_user.id in _pending_testmail:
        _pending_testmail.discard(message.from_user.id)
        await message.answer("Отменено.")
        return
    await message.answer("Нечего отменять.")


@router.message(WaitingTestMail(), F.text, CanManage())
async def on_testmail_address(message: Message, ctx: Ctx) -> None:
    _pending_testmail.discard(message.from_user.id)
    await _send_test_mail(message, ctx, message.text or "")


@router.message(Command("myphone"), CanManage())
async def cmd_myphone(message: Message, ctx: Ctx, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if raw:
        phone = normalize_phone(raw)
        if not phone:
            await message.answer("Не похоже на номер. Пример: /myphone +79131234567")
            return
        await ctx.storage.set_admin_phone(message.from_user.id, phone)
        await message.answer(
            f"Номер сохранён: {format_phone(phone)}\n"
            "В письме на кухню уйдёт он, если заказ отправите вы.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    current = await ctx.storage.get_admin_phone(message.from_user.id)
    now = f"Сейчас: {format_phone(current)}" if current else "Номер ещё не задан."
    await message.answer(
        f"{now}\n\nНажмите кнопку ниже или напишите /myphone +79131234567",
        reply_markup=phone_keyboard(),
    )


@router.message(Command("mymail"), CanManage())
async def cmd_mymail(message: Message, ctx: Ctx, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if raw.lower() in {"clear", "удалить", "-", "none", "off"}:
        await ctx.storage.clear_admin_email(message.from_user.id)
        await message.answer("Почта для копии письма снята.")
        return
    if raw:
        mail = raw.lower()
        if not _EMAIL_RE.match(mail):
            await message.answer("Не похоже на email. Пример: /mymail name@mail.ru")
            return
        await ctx.storage.set_admin_email(message.from_user.id, mail)
        await message.answer(
            f"Почта сохранена: {mail}\n"
            "При /send она уйдёт в копию (Cc) письма на кухню."
        )
        return
    current = await ctx.storage.get_admin_email(message.from_user.id)
    if current:
        await message.answer(
            f"Сейчас в копии: {current}\n\n"
            "Сменить: /mymail name@mail.ru\n"
            "Убрать: /mymail clear"
        )
    else:
        await message.answer(
            "Почта для копии ещё не задана.\n"
            "Пример: /mymail name@mail.ru"
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
        await message.answer("Не удалось разобрать номер. Напишите /myphone +79131234567")
        return
    await ctx.storage.set_admin_phone(message.from_user.id, phone)
    await message.answer(
        f"Номер сохранён: {format_phone(phone)}\n"
        "В письме на кухню уйдёт он, если заказ отправите вы.",
        reply_markup=ReplyKeyboardRemove(),
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
        return
    if smtp_configured(ctx.settings):
        return
    day = today_in_tz(ctx.settings)
    try:
        menu = await ctx.cache.get(day)
        orders = await ctx.storage.list_orders(day)
    except Exception as exc:
        await message.answer(
            "SMTP не настроен — письмо на кухню пока не уйдёт.\n"
            f"Текст письма показать не смог: {exc}"
        )
        return
    phone = await ctx.storage.get_admin_phone(message.from_user.id)
    body = email_body(
        menu,
        orders,
        address=ctx.settings.delivery_address or "(DELIVERY_ADDRESS не задан)",
        address_comment=ctx.settings.delivery_comment,
        contact_name=user_label(message.from_user),
        contact_phone=format_phone(phone) if phone else "(номер не задан, /myphone)",
    )
    await answer_long(
        message,
        "SMTP не настроен (SMTP_USER / SMTP_PASSWORD). "
        "Письмо на mail@edatomsk.ru пока не уйдёт.\n\n"
        "Текст письма:\n\n" + body,
    )


@router.message(Command("summary"), CanManage())
async def cmd_summary(message: Message, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    try:
        summary, xls, filename, orders = await build_day_package(ctx, day, refresh=True)
    except Exception as exc:
        await message.answer(f"Не удалось получить меню с сайта: {exc}")
        return
    await message.answer_document(BufferedInputFile(xls, filename=filename))
    await answer_summary(message, summary or "Пусто")
    if not ctx.cache.meta_ok:
        await message.answer("Не удалось проверить доступность блюд на сайте.")
        return
    bad = unavailable_in_orders(await ctx.cache.get(day), orders)
    if bad:
        await message.answer(format_unavailable_report(bad, sending=False))


@router.message(Command("status", "missing"), CanManage())
async def cmd_status(message: Message, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    orders = await ctx.storage.list_orders(day)
    ordered_map = {uid: name for uid, name, _ in orders}
    skipped = dict(await ctx.storage.list_skips(day))
    roster = await ctx.storage.list_roster()
    ordered_names: list[str] = []
    silent: list[str] = []
    skipped_names: list[str] = []
    still_in = 0
    seen: set[int] = set()
    if ctx.settings.channel_id:
        candidates = list(roster)
        for uid, name in ordered_map.items():
            candidates.append((uid, name))
        for uid, name in skipped.items():
            candidates.append((uid, name))
        for uid, name in candidates:
            if uid in seen:
                continue
            if not await is_channel_member(message.bot, ctx.settings.channel_id, uid):
                continue
            seen.add(uid)
            still_in += 1
            if uid in ordered_map:
                ordered_names.append(ordered_map[uid] or name)
            elif uid in skipped:
                skipped_names.append(skipped[uid] or name)
            else:
                silent.append(name)
        ordered_names.sort(key=str.casefold)
        skipped_names.sort(key=str.casefold)
        silent.sort(key=str.casefold)
    channel_total = 0
    if ctx.settings.channel_id:
        try:
            channel_total = await message.bot.get_chat_member_count(ctx.settings.channel_id)
        except Exception:
            log.exception("Не удалось получить число подписчиков канала")
    note = (
        "Telegram не отдаёт имена всех подписчиков канала, только их число. "
        "Бот запоминает тех, кто вошёл в канал, пока он админ, плюс кто открывал меню или писал в личку."
    )
    if channel_total:
        note = (
            f"Подписчиков в канале: {channel_total}. "
            f"Известно боту и всё ещё в канале: {still_in}.\n{note}"
        )
    if not roster and not skipped and not ordered_map:
        await message.answer(note + "\n\nПока никого в списке нет.")
        return

    def block(title: str, names: list[str]) -> str:
        if not names:
            return f"{title} (0): —"
        return f"{title} ({len(names)}):\n" + "\n".join(names)

    body_parts = [
        block("Заказали", ordered_names),
        block("Пропустили", skipped_names),
        block("Без ответа", silent),
    ]
    await answer_summary(
        message,
        "\n\n".join(body_parts),
        lead=note,
    )


@router.message(Command("summary_clear"), CanManage())
async def cmd_summary_clear(message: Message, ctx: Ctx) -> None:
    day = today_in_tz(ctx.settings)
    try:
        summary, _, _, orders = await build_day_package(ctx, day, refresh=True)
    except Exception as exc:
        await message.answer(f"Не удалось получить меню с сайта: {exc}")
        return
    if not orders:
        await message.answer("Заказов нет, очищать нечего.")
        return
    trail = ""
    if ctx.cache.meta_ok:
        bad = unavailable_in_orders(await ctx.cache.get(day), orders)
        if bad:
            trail = format_unavailable_report(bad, sending=False)
    await answer_summary(
        message,
        summary or "Пусто",
        lead=(
            "Выгрузить XLS и стереть все заказы за сегодня?\n"
            "Письмо на кухню не отправляю, сбор не закрываю."
        ),
        trail=trail,
        reply_markup=summary_clear_keyboard(day),
    )


@router.callback_query(F.data == "clear:cancel")
async def cb_summary_clear_cancel(cb: CallbackQuery, ctx: Ctx) -> None:
    if not await can_manage(cb.bot, ctx, cb.from_user):
        await cb.answer()
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Отменено")


@router.callback_query(F.data.startswith("clear:"))
async def cb_summary_clear(cb: CallbackQuery, ctx: Ctx) -> None:
    if not await can_manage(cb.bot, ctx, cb.from_user):
        await cb.answer()
        return
    payload = cb.data.split(":", 1)[1]
    if payload == "cancel":
        return
    day = date.fromisoformat(payload)
    try:
        summary, xls, filename, orders = await build_day_package(ctx, day, refresh=True)
    except Exception as exc:
        await cb.message.answer(f"Не удалось получить меню с сайта: {exc}")
        await cb.answer("Ошибка", show_alert=True)
        return
    if not orders:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.message.answer("Заказов уже нет.")
        await cb.answer("Пусто")
        return
    try:
        await cb.message.answer_document(BufferedInputFile(xls, filename=filename))
        await answer_summary(
            cb.message,
            summary or "Пусто",
            trail="Заказы очищены.",
        )
    except Exception as exc:
        log.exception("summary_clear document failed")
        await cb.message.answer(f"Не удалось отправить файл, заказы не трогал: {exc}")
        await cb.answer("Ошибка", show_alert=True)
        return
    await ctx.storage.clear_orders(day)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("Очищено")


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
            "Для письма нужен ваш контактный номер. Нажмите кнопку или напишите /myphone +79131234567",
            reply_markup=phone_keyboard(),
        )
        return
    bad = unavailable_in_orders(await ctx.cache.get(day), orders)
    if bad:
        await message.answer(format_unavailable_report(bad), reply_markup=drop_keyboard(day))
        return
    await answer_summary(
        message,
        summary or "Пусто",
        lead="Отправить лист заказа на mail@edatomsk.ru?",
        reply_markup=send_keyboard(day),
    )


@router.callback_query(F.data == "send:cancel")
async def cb_cancel(cb: CallbackQuery, ctx: Ctx) -> None:
    if not await can_manage(cb.bot, ctx, cb.from_user):
        await cb.answer()
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Отменено")


@router.callback_query(F.data.startswith("send:drop:"))
async def cb_drop_unavailable(cb: CallbackQuery, ctx: Ctx) -> None:
    if not await can_manage(cb.bot, ctx, cb.from_user):
        await cb.answer()
        return
    day = date.fromisoformat(cb.data.rsplit(":", 1)[1])
    if await ctx.storage.is_sent(day):
        await cb.answer("Уже отправлено")
        return
    try:
        menu = await ctx.cache.get(day, force=True)
    except Exception as exc:
        await cb.message.answer(f"Не удалось получить меню с сайта: {exc}")
        await cb.answer("Ошибка", show_alert=True)
        return
    if not ctx.cache.meta_ok:
        await cb.message.answer("Не удалось получить актуальное меню с сайта. Ничего не менял.")
        await cb.answer("Нет меню", show_alert=True)
        return
    changes: list[tuple[int, dict[int, int], list[str]]] = []
    for user_id, name, items in await ctx.storage.list_orders(day):
        kept, dropped = strip_unavailable(menu, items)
        if not dropped:
            continue
        if kept:
            await ctx.storage.upsert_order(day, user_id, name, kept)
        else:
            await ctx.storage.delete_order(day, user_id)
        changes.append((user_id, kept, dropped))
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not changes:
        await cb.answer("Снятых позиций уже нет")
    else:
        await cb.answer("Убрал снятые")
    for user_id, kept, dropped in changes:
        try:
            await cb.bot.send_message(user_id, format_dropped_notice(menu, kept, dropped))
        except (TelegramForbiddenError, TelegramBadRequest):
            log.info("Не удалось написать пользователю %s про снятые блюда", user_id)
        except Exception:
            log.exception("Не удалось написать пользователю %s про снятые блюда", user_id)
    orders = await ctx.storage.list_orders(day)
    if not orders:
        await cb.message.answer("После снятия позиций заказов не осталось. Письмо не отправлял.")
        return
    bad = unavailable_in_orders(menu, orders)
    if bad:
        await cb.message.answer(format_unavailable_report(bad), reply_markup=drop_keyboard(day))
        return
    summary = format_summary(menu, orders)
    note = (
        f"Убрал снятые блюда ({sum(len(d) for _, _, d in changes)} поз. у {len(changes)}).\n\n"
        if changes
        else ""
    )
    await answer_summary(
        cb.message,
        summary or "Пусто",
        lead=note + "Отправить лист заказа на mail@edatomsk.ru?",
        reply_markup=send_keyboard(day),
    )


@router.callback_query(F.data.startswith("send:"))
async def cb_send(cb: CallbackQuery, ctx: Ctx) -> None:
    if not await can_manage(cb.bot, ctx, cb.from_user):
        await cb.answer()
        return
    payload = cb.data.split(":", 1)[1]
    if payload == "cancel" or payload.startswith("drop:"):
        return
    day = date.fromisoformat(payload)
    if await ctx.storage.is_sent(day):
        await cb.answer("Уже отправлено")
        return
    if not ctx.settings.delivery_address:
        await cb.message.answer("В .env нет DELIVERY_ADDRESS. Без адреса доставки письмо не отправлю.")
        await cb.answer("Нет адреса", show_alert=True)
        return
    if not await ctx.storage.get_admin_phone(cb.from_user.id):
        await cb.message.answer(
            "Для письма нужен ваш контактный номер. Нажмите кнопку или напишите /myphone +79131234567",
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
        day = date.fromisoformat(cb.data.split(":", 1)[1])
        await cb.message.answer(str(exc), reply_markup=drop_keyboard(day))
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
        raise RuntimeError("Сначала сохраните номер: /myphone")
    cc = await ctx.storage.get_admin_email(sender.id)
    bad = unavailable_in_orders(menu, orders)
    if bad:
        raise UnavailableItemsError(format_unavailable_report(bad))
    people = [(name, items) for _, name, items in orders]
    xls = await build_filled_xls(menu, people)
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
        note = f"\nCc: {cc}" if cc else ""
        await bot.send_message(sender.id, "Текст письма:\n\n" + body[:3500] + note)
    else:
        await asyncio.to_thread(
            send_order_email,
            ctx.settings,
            subject,
            body,
            xls,
            filename,
            cc=cc,
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
        await send_summary(bot, ctx.settings.channel_id, format_summary(menu, orders))
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
    markup = send_keyboard(day) if orders else None
    for uid in recipients:
        try:
            await send_summary(
                bot,
                uid,
                summary or "Пусто",
                lead="Дедлайн. Сбор закрыт.",
                reply_markup=markup,
            )
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
