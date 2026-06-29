"""Async SQLite storage for per-guild stock-webhook subscriptions + settings."""
import os
import time

import aiosqlite

# Columns a guild may tune via /webhook-settings.
_SETTING_KEYS = {"interval_minutes", "cap", "games", "show_zero", "title"}


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
        await self._migrate()
        await self._conn.commit()

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self):
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_subscriptions (
                guild_id         INTEGER PRIMARY KEY,
                webhook_url      TEXT NOT NULL,
                added_by         INTEGER,
                created_at       INTEGER NOT NULL,
                interval_minutes INTEGER,
                cap              INTEGER,
                games            TEXT,
                show_zero        INTEGER DEFAULT 1,
                title            TEXT,
                last_sent_at     INTEGER DEFAULT 0
            )
            """
        )

    async def _migrate(self):
        """Add any columns missing from older databases."""
        cur = await self._conn.execute("PRAGMA table_info(stock_subscriptions)")
        cols = {row["name"] for row in await cur.fetchall()}
        adds = {
            "interval_minutes": "INTEGER",
            "cap": "INTEGER",
            "games": "TEXT",
            "show_zero": "INTEGER DEFAULT 1",
            "title": "TEXT",
            "last_sent_at": "INTEGER DEFAULT 0",
        }
        for name, decl in adds.items():
            if name not in cols:
                await self._conn.execute(
                    f"ALTER TABLE stock_subscriptions ADD COLUMN {name} {decl}"
                )

    # ----- subscriptions -----
    async def set_subscription(self, guild_id, webhook_url, added_by):
        """Insert or replace the webhook for a guild, preserving its settings."""
        await self._conn.execute(
            """
            INSERT INTO stock_subscriptions (guild_id, webhook_url, added_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                webhook_url = excluded.webhook_url,
                added_by    = excluded.added_by
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

    async def update_settings(self, guild_id, **fields):
        cols = {k: v for k, v in fields.items() if k in _SETTING_KEYS}
        if not cols:
            return
        assignments = ", ".join(f"{k} = ?" for k in cols)
        await self._conn.execute(
            f"UPDATE stock_subscriptions SET {assignments} WHERE guild_id = ?",
            (*cols.values(), guild_id),
        )
        await self._conn.commit()

    async def mark_sent(self, guild_id, when=None):
        await self._conn.execute(
            "UPDATE stock_subscriptions SET last_sent_at = ? WHERE guild_id = ?",
            (int(when if when is not None else time.time()), guild_id),
        )
        await self._conn.commit()
