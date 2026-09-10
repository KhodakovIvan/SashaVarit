from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import aiosqlite


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (day, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS days (
                    day TEXT PRIMARY KEY,
                    closed INTEGER NOT NULL DEFAULT 0,
                    sent INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS dish_names (
                    dish_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_contacts (
                    user_id INTEGER PRIMARY KEY,
                    phone TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS roster (
                    user_id INTEGER PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS skips (
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (day, user_id)
                )
                """
            )
            await db.commit()

    async def upsert_order(self, day: date, user_id: int, user_name: str, items: dict[int, int]) -> None:
        clean = {str(k): int(v) for k, v in items.items() if int(v) > 0}
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO orders(day, user_id, user_name, items_json, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(day, user_id) DO UPDATE SET
                    user_name=excluded.user_name,
                    items_json=excluded.items_json,
                    updated_at=datetime('now')
                """,
                (day.isoformat(), user_id, user_name, json.dumps(clean, ensure_ascii=False)),
            )
            await db.execute(
                "DELETE FROM skips WHERE day=? AND user_id=?",
                (day.isoformat(), int(user_id)),
            )
            await db.execute("INSERT OR IGNORE INTO days(day) VALUES (?)", (day.isoformat(),))
            await db.commit()

    async def get_order(self, day: date, user_id: int) -> dict[int, int]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT items_json FROM orders WHERE day=? AND user_id=?",
                (day.isoformat(), user_id),
            )
            row = await cur.fetchone()
        if not row:
            return {}
        raw = json.loads(row[0])
        return {int(k): int(v) for k, v in raw.items()}

    async def list_orders(self, day: date) -> list[tuple[int, str, dict[int, int]]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT user_id, user_name, items_json FROM orders WHERE day=? ORDER BY user_name, user_id",
                (day.isoformat(),),
            )
            rows = await cur.fetchall()
        result = []
        for user_id, user_name, items_json in rows:
            items = {int(k): int(v) for k, v in json.loads(items_json).items()}
            if items:
                result.append((int(user_id), user_name, items))
        return result

    async def delete_order(self, day: date, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM orders WHERE day=? AND user_id=?",
                (day.isoformat(), user_id),
            )
            await db.commit()

    async def clear_orders(self, day: date) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM orders WHERE day=?", (day.isoformat(),))
            await db.execute("DELETE FROM skips WHERE day=?", (day.isoformat(),))
            await db.commit()

    async def set_skip(self, day: date, user_id: int, user_name: str) -> None:
        name = (user_name or "").strip() or str(int(user_id))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM orders WHERE day=? AND user_id=?",
                (day.isoformat(), int(user_id)),
            )
            await db.execute(
                """
                INSERT INTO skips(day, user_id, user_name, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(day, user_id) DO UPDATE SET
                    user_name=excluded.user_name,
                    updated_at=datetime('now')
                """,
                (day.isoformat(), int(user_id), name),
            )
            await db.execute("INSERT OR IGNORE INTO days(day) VALUES (?)", (day.isoformat(),))
            await db.commit()

    async def clear_skip(self, day: date, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM skips WHERE day=? AND user_id=?",
                (day.isoformat(), int(user_id)),
            )
            await db.commit()

    async def list_skips(self, day: date) -> list[tuple[int, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT user_id, user_name FROM skips WHERE day=? ORDER BY user_name, user_id",
                (day.isoformat(),),
            )
            rows = await cur.fetchall()
        return [(int(user_id), str(user_name)) for user_id, user_name in rows]

    async def is_skip(self, day: date, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM skips WHERE day=? AND user_id=?",
                (day.isoformat(), int(user_id)),
            )
            row = await cur.fetchone()
        return bool(row)

    async def is_closed(self, day: date) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT closed FROM days WHERE day=?", (day.isoformat(),))
            row = await cur.fetchone()
        return bool(row and row[0])

    async def set_closed(self, day: date, closed: bool = True) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR IGNORE INTO days(day) VALUES (?)", (day.isoformat(),))
            await db.execute("UPDATE days SET closed=? WHERE day=?", (1 if closed else 0, day.isoformat()))
            await db.commit()

    async def is_sent(self, day: date) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT sent FROM days WHERE day=?", (day.isoformat(),))
            row = await cur.fetchone()
        return bool(row and row[0])

    async def upsert_dish_names(self, names: dict[int, str]) -> None:
        rows = [(int(dish_id), name.strip()) for dish_id, name in names.items() if str(name).strip()]
        if not rows:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT INTO dish_names(dish_id, name) VALUES (?, ?)
                ON CONFLICT(dish_id) DO UPDATE SET name=excluded.name
                """,
                rows,
            )
            await db.commit()

    async def get_dish_names(self) -> dict[int, str]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT dish_id, name FROM dish_names")
            rows = await cur.fetchall()
        return {int(dish_id): str(name) for dish_id, name in rows}

    async def upsert_roster(self, user_id: int, user_name: str) -> None:
        name = (user_name or "").strip() or str(int(user_id))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO roster(user_id, user_name, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    user_name=excluded.user_name,
                    updated_at=datetime('now')
                """,
                (int(user_id), name),
            )
            await db.commit()

    async def list_roster(self) -> list[tuple[int, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT user_id, user_name FROM roster ORDER BY user_name, user_id"
            )
            rows = await cur.fetchall()
        return [(int(user_id), str(user_name)) for user_id, user_name in rows]

    async def delete_roster(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM roster WHERE user_id=?", (int(user_id),))
            await db.commit()

    async def set_admin_phone(self, user_id: int, phone: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO admin_contacts(user_id, phone, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    phone=excluded.phone,
                    updated_at=datetime('now')
                """,
                (int(user_id), phone),
            )
            await db.commit()

    async def get_admin_phone(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT phone FROM admin_contacts WHERE user_id=?",
                (int(user_id),),
            )
            row = await cur.fetchone()
        if not row:
            return None
        phone = str(row[0]).strip()
        return phone or None

    async def set_sent(self, day: date, sent: bool = True) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR IGNORE INTO days(day) VALUES (?)", (day.isoformat(),))
            await db.execute("UPDATE days SET sent=? WHERE day=?", (1 if sent else 0, day.isoformat()))
            await db.commit()
