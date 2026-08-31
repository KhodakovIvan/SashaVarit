from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from io import BytesIO
from urllib.parse import urlencode

import aiohttp
import xlrd
import xlwt

XLS_URL = "https://old.edatomsk.ru/backend/toExcel"
ITEMS_URL = "https://www.edatomsk.ru/backend/srvItemsList.php"
MENU_PAGE_URL = "https://www.edatomsk.ru/"
PHOTO_BASE = "https://www.edatomsk.ru/images/delivery/items/"
DISH_RE = re.compile(r"^\{(\d+)\}\s*(.+)$", re.DOTALL)
WEIGHT_IN_NAME_RE = re.compile(r"\(([^)]*г[^)]*)\)\s*$")
ITEM_BLOCK_RE = re.compile(
    r'<div class="menulistItem"(?:\s+id="\d+")?>(.*?)(?=<div class="menulistItem"|$)',
    re.DOTALL | re.IGNORECASE,
)
USER_AGENT = "SashaVarit/1.0 (office lunch bot)"


@dataclass
class Dish:
    id: int
    raw_name: str
    short_name: str
    price: float
    weighty: bool = False
    available: bool = True
    weight: str = ""
    description: str = ""
    photo_url: str = ""
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    kcal: float | None = None


@dataclass
class Category:
    name: str
    dishes: list[Dish] = field(default_factory=list)


@dataclass
class DayMenu:
    date_key: str
    title: str
    categories: list[Category]
    dishes_by_id: dict[int, Dish]


def site_date_key(year: int, month: int, day: int) -> str:
    return f"{year}.{month}.{day}"


def _short_name(rest: str) -> str:
    return rest.split(" (")[0].replace("\n", " ").replace("\r", " ").strip()


def _weight_from_raw(raw: str) -> str:
    match = WEIGHT_IN_NAME_RE.search(raw.replace("\n", " ").replace("\r", " "))
    return match.group(1).strip() if match else ""


def _to_roman(n: int) -> str:
    pairs = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    out = []
    for value, numeral in pairs:
        while n >= value:
            out.append(numeral)
            n -= value
    return "".join(out)


async def download_xls(date_key: str, persons: int) -> bytes:
    params = urlencode({"date": date_key, "orders": persons})
    url = f"{XLS_URL}?{params}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


def parse_menu(xls_bytes: bytes, date_key: str) -> DayMenu:
    book = xlrd.open_workbook(file_contents=xls_bytes, ignore_workbook_corruption=True)
    sheet = book.sheet_by_index(0)
    title = str(sheet.cell_value(0, 0) or sheet.name)
    categories: list[Category] = []
    current: Category | None = None
    dishes_by_id: dict[int, Dish] = {}

    for row in range(sheet.nrows):
        raw = str(sheet.cell_value(row, 0) or "").strip()
        if not raw:
            continue
        match = DISH_RE.match(raw)
        if match:
            if current is None:
                current = Category(name="Меню")
                categories.append(current)
            dish = Dish(
                id=int(match.group(1)),
                raw_name=raw,
                short_name=_short_name(match.group(2)),
                price=float(sheet.cell_value(row, 1) or 0),
                weighty="*" in match.group(2)[:3],
                weight=_weight_from_raw(match.group(2)),
            )
            current.dishes.append(dish)
            dishes_by_id[dish.id] = dish
            continue
        if row == 0:
            continue
        if raw in {"Наименование блюда", "Итого"}:
            continue
        current = Category(name=raw)
        categories.append(current)

    categories = [cat for cat in categories if cat.dishes]
    return DayMenu(date_key=date_key, title=title, categories=categories, dishes_by_id=dishes_by_id)


async def download_items_meta(date_key: str) -> dict[int, dict]:
    params = urlencode({"date": date_key})
    url = f"{ITEMS_URL}?{params}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw = await resp.json(content_type=None)
    return {int(k): v for k, v in raw.items()}


def apply_item_meta(menu: DayMenu, meta: dict[int, dict]) -> None:
    for dish_id, dish in menu.dishes_by_id.items():
        info = meta.get(dish_id) or {}
        if info.get("weighty"):
            dish.weighty = True
        hide = info.get("hide")
        available_flag = info.get("available")
        dish.available = not bool(hide)
        if available_flag in (0, False):
            dish.available = False


def _float(match: re.Match | None) -> float | None:
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


async def download_menu_page(date_key: str) -> str:
    params = urlencode({"date": date_key})
    url = f"{MENU_PAGE_URL}?{params}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()


def parse_menu_page(html: str) -> dict[int, dict]:
    details: dict[int, dict] = {}
    for block in ITEM_BLOCK_RE.findall(html):
        id_match = re.search(r"addToCart\((\d+)\)", block) or re.search(r"resizeMLI\((\d+)\)", block)
        if not id_match:
            continue
        dish_id = int(id_match.group(1))
        photo_match = re.search(r"/images/delivery/items/(\d+)\.jpg", block)
        desc_match = re.search(
            r'<div class="dish__description">(.*?)<div class="dish_bgu">',
            block,
            re.DOTALL,
        )
        description = ""
        if desc_match:
            description = unescape(re.sub(r"<[^>]+>", "", desc_match.group(1))).strip()
        weight_match = re.search(r'<div class="dish__weight">([^<]+)</div>', block)
        weight = ""
        if weight_match:
            weight = unescape(weight_match.group(1)).replace("\xa0", " ").strip()
        details[dish_id] = {
            "photo_url": f"{PHOTO_BASE}{photo_match.group(1)}.jpg" if photo_match else "",
            "description": description,
            "weight": weight,
            "protein": _float(re.search(r"Белки:\s*([\d.]+)", block)),
            "fat": _float(re.search(r"Жиры:\s*([\d.]+)", block)),
            "carbs": _float(re.search(r"Углеводы:\s*([\d.]+)", block)),
            "kcal": _float(re.search(r"([\d.]+)\s*ккал", block)),
        }
    return details


def apply_details(menu: DayMenu, details: dict[int, dict]) -> None:
    for dish_id, dish in menu.dishes_by_id.items():
        info = details.get(dish_id)
        if not info:
            continue
        if info.get("photo_url"):
            dish.photo_url = info["photo_url"]
        if info.get("description"):
            dish.description = info["description"]
        if info.get("weight"):
            dish.weight = info["weight"]
        dish.protein = info.get("protein")
        dish.fat = info.get("fat")
        dish.carbs = info.get("carbs")
        dish.kcal = info.get("kcal")


def menu_to_json(menu: DayMenu) -> dict:
    return {
        "date": menu.date_key,
        "title": menu.title,
        "categories": [
            {
                "name": cat.name,
                "dishes": [
                    {
                        "id": dish.id,
                        "name": dish.short_name,
                        "price": dish.price,
                        "weighty": dish.weighty,
                        "available": dish.available,
                        "weight": dish.weight,
                        "description": dish.description,
                        "photo": dish.photo_url,
                        "protein": dish.protein,
                        "fat": dish.fat,
                        "carbs": dish.carbs,
                        "kcal": dish.kcal,
                    }
                    for dish in cat.dishes
                ],
            }
            for cat in menu.categories
        ],
    }


def build_filled_xls(
    menu: DayMenu,
    people: list[tuple[str, dict[int, int]]],
) -> bytes:
    """people: [(display_name, {dish_id: qty}), ...]"""
    book = xlwt.Workbook()
    sheet = book.add_sheet("Лист заказа")

    title_style = xlwt.easyxf("font: bold on, height 240")
    header_style = xlwt.easyxf("font: bold on; align: wrap on, vert centre, horiz centre")
    cat_style = xlwt.easyxf("font: bold on")
    money_style = xlwt.easyxf("align: horiz right")
    qty_style = xlwt.easyxf("align: horiz centre")

    n = len(people)
    sheet.write(0, 0, menu.title, title_style)
    sheet.write(1, 0, "Наименование блюда", header_style)
    sheet.write(1, 1, "Цена", header_style)
    for i, (name, _) in enumerate(people):
        label = name or _to_roman(i + 1)
        sheet.write(1, 2 + i, label, header_style)
        sheet.col(2 + i).width = 4000
    sheet.write(1, 2 + n, "Итого", header_style)
    sheet.col(0).width = 18000
    sheet.col(1).width = 2500
    sheet.col(2 + n).width = 3000

    row = 2
    person_totals = [0.0] * n
    for cat in menu.categories:
        sheet.write(row, 0, cat.name, cat_style)
        for i in range(n):
            sheet.write(row, 2 + i, _to_roman(i + 1), cat_style)
        row += 1
        for dish in cat.dishes:
            label = dish.raw_name
            if dish.weighty and not label.lstrip().startswith("*"):
                label = "* " + label
            sheet.write(row, 0, label)
            sheet.write(row, 1, f"~{int(dish.price)}" if dish.weighty else dish.price, money_style)
            line_qty = 0
            for i, (_, items) in enumerate(people):
                qty = int(items.get(dish.id, 0) or 0)
                if qty:
                    sheet.write(row, 2 + i, qty, qty_style)
                    person_totals[i] += qty * dish.price
                    line_qty += qty
            sheet.write(row, 2 + n, line_qty, qty_style)
            row += 1

    sheet.write(row, 0, "Сумма, руб" + (" ≈" if any(d.weighty for d in menu.dishes_by_id.values()) else ""), cat_style)
    grand = 0.0
    for i, amount in enumerate(person_totals):
        sheet.write(row, 2 + i, amount, money_style)
        grand += amount
    sheet.write(row, 2 + n, grand, money_style)

    buf = BytesIO()
    book.save(buf)
    return buf.getvalue()
