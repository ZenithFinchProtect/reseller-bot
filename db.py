"""Async SQLite storage for per-guild stock-webhook subscriptions."""
import os
import time

import aiosqlite


class Database:
    def __init__(self, path="reseller_data.db"):
        self.path = path
        self._conn = None

    async def connect(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._create_tables()
        await self._conn.commit()

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self):
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_subscriptions (
                guild_id    INTEGER PRIMARY KEY,
                webhook_url TEXT NOT NULL,
                added_by    INTEGER,
                created_at  INTEGER NOT NULL
            )
            """
        )

    # ----- stock webhook subscriptions -----
    async def set_subscription(self, guild_id, webhook_url, added_by):
        """Insert or replace the stock webhook for a guild."""
        await self._conn.execute(
            """
            INSERT INTO stock_subscriptions (guild_id, webhook_url, added_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                webhook_url = excluded.webhook_url,
                added_by    = excluded.added_by,
                created_at  = excluded.created_at
            """,
            (guild_id, webhook_url, added_by, int(time.time())),
        )
        await self._conn.commit()

    async def remove_subscription(self, guild_id):
        cur = await self._conn.execute(
            "DELETE FROM stock_subscriptions WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_subscription(self, guild_id):
        cur = await self._conn.execute(
            "SELECT * FROM stock_subscriptions WHERE guild_id = ?", (guild_id,)
        )
        return await cur.fetchone()

    async def all_subscriptions(self):
        cur = await self._conn.execute("SELECT * FROM stock_subscriptions")
        return await cur.fetchall()
