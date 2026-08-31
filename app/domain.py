from __future__ import annotations

from datetime import date, datetime, time

from app.config import Settings
from app.edatomsk import DayMenu


def today_in_tz(settings: Settings) -> date:
    return datetime.now(settings.tz).date()


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def deadline_dt(settings: Settings, day: date) -> datetime:
    return datetime.combine(day, time(settings.deadline_hour, settings.deadline_minute), tzinfo=settings.tz)


def is_after_deadline(settings: Settings, day: date) -> bool:
    if not settings.deadline_enabled:
        return False
    return datetime.now(settings.tz) >= deadline_dt(settings, day)


def dish_is_available(menu: DayMenu, dish_id: int) -> bool:
    dish = menu.dishes_by_id.get(dish_id)
    return bool(dish and dish.available)


def dish_label(menu: DayMenu, dish_id: int) -> str:
    dish = menu.dishes_by_id.get(dish_id)
    if dish and dish.short_name:
        return dish.short_name
    name = (menu.names_by_id.get(dish_id) or "").strip()
    return name or f"позиция {dish_id}"


def unavailable_in_orders(
    menu: DayMenu, orders: list[tuple[int, str, dict[int, int]]]
) -> list[tuple[str, list[str]]]:
    result = []
    for _, name, items in orders:
        missing: list[str] = []
        for dish_id, qty in items.items():
            if not qty:
                continue
            if dish_is_available(menu, dish_id):
                continue
            missing.append(dish_label(menu, dish_id))
        if missing:
            result.append((name, missing))
    return result


def format_unavailable_report(rows: list[tuple[str, list[str]]], *, sending: bool = True) -> str:
    if sending:
        lines = [
            "Письмо не отправлено: на сайте нет части заказанных блюд.",
            "Сбор не закрывал, заказчики могут поправить состав. Потом снова /send.",
            "",
        ]
    else:
        lines = [
            "Недоступные позиции на сайте:",
            "",
        ]
    for name, dishes in rows:
        lines.append(name)
        for dish in dishes:
            lines.append(f"  — {dish}")
        lines.append("")
    return "\n".join(lines).strip()


def person_total(menu: DayMenu, items: dict[int, int]) -> float:
    total = 0.0
    for dish_id, qty in items.items():
        dish = menu.dishes_by_id.get(dish_id)
        if dish and qty:
            total += dish.price * qty
    return total


def has_weighty(menu: DayMenu, items: dict[int, int]) -> bool:
    return any(
        qty and (dish := menu.dishes_by_id.get(dish_id)) and dish.weighty
        for dish_id, qty in items.items()
    )


def format_items(menu: DayMenu, items: dict[int, int]) -> str:
    lines = []
    for dish_id, qty in items.items():
        if not qty:
            continue
        dish = menu.dishes_by_id.get(dish_id)
        if not dish:
            lines.append(f"  {dish_label(menu, dish_id)} × {qty} — нет на сайте")
            continue
        amount = int(dish.price * qty)
        if dish.weighty:
            line = f"  * {dish.short_name} × {qty} ≈ {amount} ₽ (вес, цена по факту)"
        else:
            line = f"  {dish.short_name} × {qty} = {amount} ₽"
        if not dish.available:
            line += " — нет на сайте"
        lines.append(line)
    return "\n".join(lines) if lines else "  пусто"


def _fmt_nutrient(value: float) -> str:
    rounded = round(value, 1)
    if abs(rounded - round(rounded)) < 0.05:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"


def person_nutrition(menu: DayMenu, items: dict[int, int]) -> tuple[float, float, float, float, bool]:
    protein = fat = carbs = kcal = 0.0
    known = False
    incomplete = False
    for dish_id, qty in items.items():
        if not qty:
            continue
        dish = menu.dishes_by_id.get(dish_id)
        if not dish:
            continue
        if dish.protein is None and dish.fat is None and dish.carbs is None and dish.kcal is None:
            incomplete = True
            continue
        known = True
        protein += (dish.protein or 0) * qty
        fat += (dish.fat or 0) * qty
        carbs += (dish.carbs or 0) * qty
        kcal += (dish.kcal or 0) * qty
    return protein, fat, carbs, kcal, known and not incomplete


def format_nutrition_line(menu: DayMenu, items: dict[int, int]) -> str | None:
    protein, fat, carbs, kcal, complete = person_nutrition(menu, items)
    known = protein or fat or carbs or kcal
    if not known:
        return None
    note = ""
    if not complete:
        note = " (не по всем блюдам есть данные)"
    elif has_weighty(menu, items):
        note = " (весовые — ориентировочно)"
    return (
        f"КБЖУ: белки {_fmt_nutrient(protein)} · жиры {_fmt_nutrient(fat)} · "
        f"углеводы {_fmt_nutrient(carbs)} · {_fmt_nutrient(kcal)} ккал{note}"
    )


def _total_line(amount: float, approximate: bool) -> str:
    if approximate:
        return f"Итого ≈ {int(amount)} ₽ (есть весовые блюда, точная сумма при получении)"
    return f"Итого: {int(amount)} ₽"


def format_user_receipt(menu: DayMenu, items: dict[int, int]) -> str:
    amount = person_total(menu, items)
    if not items:
        return "Заказ очищен."
    lines = ["Заказ сохранён.", ""]
    body = format_items(menu, items)
    if body:
        lines.append(body.lstrip())
        lines.append("")
    lines.append(_total_line(amount, has_weighty(menu, items)))
    nutrition = format_nutrition_line(menu, items)
    if nutrition:
        lines.append(nutrition)
    return "\n".join(lines)


def format_summary(menu: DayMenu, orders: list[tuple[int, str, dict[int, int]]]) -> str:
    if not orders:
        return "Заказов пока нет."
    blocks = []
    grand = 0.0
    any_weighty = False
    for _, name, items in orders:
        amount = person_total(menu, items)
        grand += amount
        approx = has_weighty(menu, items)
        any_weighty = any_weighty or approx
        mark = " ≈" if approx else ""
        blocks.append(f"{name}:{mark} {int(amount)} ₽\n{format_items(menu, items)}")
    blocks.append(_total_line(grand, any_weighty) + f", персон: {len(orders)}")
    if any_weighty:
        blocks.append("* весовое блюдо: цена ориентировочная, итог известен при получении.")
    return "\n\n".join(blocks)


def email_body(
    menu: DayMenu,
    orders: list[tuple[int, str, dict[int, int]]],
    *,
    address: str,
    address_comment: str = "",
    contact_name: str,
    contact_phone: str,
) -> str:
    header = [
        menu.title,
        f"Персон: {len(orders)}",
        "",
        f"Доставка: {address}",
    ]
    if address_comment:
        header.append(address_comment)
    header.append(f"Контакт: {contact_name}, {contact_phone}")
    return (
        "\n".join(header)
        + "\n\n"
        + f"{format_summary(menu, orders)}\n\n"
        "Лист заказа во вложении.\n"
        "Отправлено ботом SashaVarit."
    )
