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
    names_by_id: dict[int, str] = field(default_factory=dict)


def site_date_key(year: int, month: int, day: int) -> str:
    return f"{year}.{month}.{day}"


def _short_name(rest: str) -> str:
    return rest.split(" (")[0].replace("\n", " ").replace("\r", " ").strip()


def _weight_from_raw(raw: str) -> str:
    match = WEIGHT_IN_NAME_RE.search(raw.replace("\n", " ").replace("\r", " "))
    return match.group(1).strip() if match else ""


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
    names_by_id: dict[int, str] = {}

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
            names_by_id[dish.id] = dish.short_name
            continue
        if row == 0:
            continue
        if raw in {"Наименование блюда", "Итого"}:
            continue
        current = Category(name=raw)
        categories.append(current)

    categories = [cat for cat in categories if cat.dishes]
    return DayMenu(
        date_key=date_key,
        title=title,
        categories=categories,
        dishes_by_id=dishes_by_id,
        names_by_id=names_by_id,
    )


async def download_items_meta(date_key: str) -> dict[int, dict]:
    params = urlencode({"date": date_key})
    url = f"{ITEMS_URL}?{params}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw = await resp.json(content_type=None)
    return {int(k): v for k, v in raw.items()}


def _meta_name(info: dict) -> str:
    raw = unescape(str(info.get("name") or "")).replace("\xa0", " ").strip()
    return raw


def apply_item_meta(menu: DayMenu, meta: dict[int, dict]) -> None:
    for dish_id, info in meta.items():
        name = _meta_name(info)
        if name:
            menu.names_by_id.setdefault(dish_id, name)
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


def _copy_cell(sheet: xlwt.Worksheet, row: int, col: int, src) -> None:
    ctype = src.cell_type(row, col)
    if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return
    value = src.cell_value(row, col)
    if ctype == xlrd.XL_CELL_ERROR:
        return
    if ctype == xlrd.XL_CELL_BOOLEAN:
        sheet.write(row, col, bool(value))
        return
    if ctype == xlrd.XL_CELL_NUMBER:
        sheet.write(row, col, value)
        return
    if value == "":
        return
    sheet.write(row, col, value)


def _totals_row_index(src) -> int | None:
    for row in range(src.nrows - 1, -1, -1):
        if str(src.cell_value(row, 0) or "").strip():
            return None
        for col in range(2, src.ncols):
            if src.cell_type(row, col) == xlrd.XL_CELL_NUMBER:
                return row
    return None


def fill_xls_template(xls_bytes: bytes, people: list[tuple[str, dict[int, int]]]) -> bytes:
    """Fill official site XLS: keep sheet/headers/{id} names, write qty and sums as numbers."""
    n = len(people)
    src_book = xlrd.open_workbook(file_contents=xls_bytes, ignore_workbook_corruption=True)
    src = src_book.sheet_by_index(0)
    first_person_col = 2
    total_col = 2 + n
    if n and src.ncols < total_col + 1:
        raise RuntimeError(
            f"В шаблоне XLS {src.ncols} колонок, нужно минимум {total_col + 1} для {n} персон"
        )

    money_style = xlwt.easyxf("align: horiz right")
    qty_style = xlwt.easyxf("align: horiz centre")
    out = xlwt.Workbook(encoding="utf-8")
    sheet = out.add_sheet(src.name[:31])
    sheet.col(0).width = 18000
    sheet.col(1).width = 2500
    for i in range(n):
        sheet.col(first_person_col + i).width = 4000
    if n:
        sheet.col(total_col).width = 3000

    totals_row = _totals_row_index(src)
    person_sums = [0.0] * n
    grand = 0.0

    for row in range(src.nrows):
        raw = str(src.cell_value(row, 0) or "").strip()
        dish = DISH_RE.match(raw)
        is_totals = totals_row is not None and row == totals_row
        for col in range(src.ncols):
            if dish and n and first_person_col <= col <= first_person_col + n - 1:
                continue
            if dish and n and col == total_col:
                continue
            if is_totals and n and col >= first_person_col:
                continue
            _copy_cell(sheet, row, col, src)
        if not dish or not n:
            continue
        price = float(src.cell_value(row, 1) or 0)
        dish_id = int(dish.group(1))
        row_sum = 0.0
        for i, (_, items) in enumerate(people):
            qty = int(items.get(dish_id, 0) or 0)
            if not qty:
                continue
            sheet.write(row, first_person_col + i, qty, qty_style)
            amount = qty * price
            row_sum += amount
            person_sums[i] += amount
        sheet.write(row, total_col, row_sum, money_style)
        grand += row_sum

    if n and totals_row is not None:
        for i, amount in enumerate(person_sums):
            sheet.write(totals_row, first_person_col + i, amount, money_style)
        sheet.write(totals_row, total_col, grand, money_style)

    buf = BytesIO()
    out.save(buf)
    return buf.getvalue()


async def build_filled_xls(
    menu: DayMenu,
    people: list[tuple[str, dict[int, int]]],
) -> bytes:
    """Download today's official XLS and fill person quantities."""
    n = max(len(people), 1)
    raw = await download_xls(menu.date_key, n)
    return fill_xls_template(raw, people)
