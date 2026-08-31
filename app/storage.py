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
            await db.commit()

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

    async def set_sent(self, day: date, sent: bool = True) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR IGNORE INTO days(day) VALUES (?)", (day.isoformat(),))
            await db.execute("UPDATE days SET sent=? WHERE day=?", (1 if sent else 0, day.isoformat()))
            await db.commit()
