"""Async SQLite storage: stock-webhook subscriptions plus coin balances/settings."""
import os
import time

import aiosqlite

# Columns a guild may tune via /webhook-settings.
_SETTING_KEYS = {"interval_minutes", "cap", "games", "show_zero", "title"}

# Only these coin-setting columns may be updated programmatically.
_ALLOWED_COIN_SETTING_KEYS = {
    "required_status",
    "reward_seconds",
    "coins_per_reward",
    "log_channel_id",
    "eligible_statuses",
}

# Only these user columns may be updated via upsert_user.
_ALLOWED_USER_KEYS = {
    "total_eligible_seconds",
    "coins",
    "rewards_count",
    "credited_seconds",
    "updated_at",
}


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
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coin_settings (
                guild_id INTEGER PRIMARY KEY,
                required_status TEXT,
                reward_seconds REAL,
                coins_per_reward INTEGER,
                log_channel_id INTEGER,
                eligible_statuses TEXT
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER,
                user_id INTEGER,
                total_eligible_seconds REAL DEFAULT 0,
                coins REAL DEFAULT 0,
                rewards_count INTEGER DEFAULT 0,
                credited_seconds REAL DEFAULT 0,
                updated_at REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                coins INTEGER NOT NULL,
                usd REAL NOT NULL,
                currency TEXT NOT NULL,
                amount_units INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                txid TEXT,
                created_at INTEGER NOT NULL,
                paid_at INTEGER
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_settings (
                currency TEXT PRIMARY KEY,
                payout_address TEXT,
                auto_threshold_usd REAL DEFAULT 0
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
        cur = await self._conn.execute("PRAGMA table_info(users)")
        ucols = {row["name"] for row in await cur.fetchall()}
        if ucols and "credited_seconds" not in ucols:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN credited_seconds REAL DEFAULT 0"
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

    # ----- coin settings -----
    async def get_coin_settings(self, guild_id, defaults):
        cur = await self._conn.execute(
            "SELECT * FROM coin_settings WHERE guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        if row is None:
            await self._conn.execute(
                """INSERT OR IGNORE INTO coin_settings
                   (guild_id, required_status, reward_seconds, coins_per_reward,
                    log_channel_id, eligible_statuses)
                   VALUES (?,?,?,?,?,?)""",
                (
                    guild_id,
                    defaults["required_status"],
                    defaults["reward_seconds"],
                    defaults["coins_per_reward"],
                    defaults["log_channel_id"],
                    defaults["eligible_statuses"],
                ),
            )
            await self._conn.commit()
            out = dict(defaults)
            out["guild_id"] = guild_id
            return out
        return dict(row)

    async def update_coin_setting(self, guild_id, key, value):
        if key not in _ALLOWED_COIN_SETTING_KEYS:
            raise ValueError(f"Illegal setting key: {key}")
        await self._conn.execute(
            f"UPDATE coin_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id)
        )
        await self._conn.commit()

    # ----- users / coins -----
    async def get_user(self, guild_id, user_id):
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            now = time.time()
            await self._conn.execute(
                "INSERT INTO users (guild_id, user_id, updated_at) VALUES (?,?,?)",
                (guild_id, user_id, now),
            )
            await self._conn.commit()
            return {
                "guild_id": guild_id,
                "user_id": user_id,
                "total_eligible_seconds": 0.0,
                "coins": 0,
                "rewards_count": 0,
                "credited_seconds": 0.0,
                "updated_at": now,
            }
        return dict(row)

    async def upsert_user(self, guild_id, user_id, **fields):
        for k in fields:
            if k not in _ALLOWED_USER_KEYS:
                raise ValueError(f"Illegal user key: {k}")
        await self.get_user(guild_id, user_id)  # ensure row exists
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [guild_id, user_id]
        await self._conn.execute(
            f"UPDATE users SET {cols} WHERE guild_id=? AND user_id=?", vals
        )
        await self._conn.commit()

    async def add_coins(self, guild_id, user_id, amount):
        u = await self.get_user(guild_id, user_id)
        new = max(0, u["coins"] + amount)
        await self.upsert_user(guild_id, user_id, coins=new)
        return new

    async def set_coins(self, guild_id, user_id, amount):
        new = max(0, amount)
        await self.upsert_user(guild_id, user_id, coins=new)
        return new

    async def try_adjust_coins(self, guild_id, user_id, delta):
        """Atomically add `delta` coins, refusing to go below zero.

        Returns (ok, balance). When the change would make the balance
        negative, nothing is written and (False, current_balance) is
        returned.
        """
        u = await self.get_user(guild_id, user_id)
        current = u["coins"]
        new = current + delta
        if new < 0:
            return False, current
        await self.upsert_user(guild_id, user_id, coins=new)
        return True, new

    # ----- crypto top-ups -----
    async def create_topup(self, guild_id, user_id, coins, usd, currency, amount_units):
        cur = await self._conn.execute(
            "INSERT INTO topups (guild_id, user_id, coins, usd, currency, amount_units, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (guild_id, user_id, coins, usd, currency, amount_units, int(time.time())),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def pending_topups(self, currency=None):
        q = "SELECT * FROM topups WHERE status='pending'"
        args = ()
        if currency:
            q += " AND currency=?"
            args = (currency,)
        cur = await self._conn.execute(q, args)
        return await cur.fetchall()

    async def pending_amount_exists(self, currency, amount_units):
        cur = await self._conn.execute(
            "SELECT 1 FROM topups WHERE status='pending' AND currency=? AND amount_units=?",
            (currency, amount_units),
        )
        return await cur.fetchone() is not None

    async def mark_topup(self, topup_id, status, txid=None):
        cur = await self._conn.execute(
            "UPDATE topups SET status=?, txid=?, paid_at=? WHERE id=? AND status='pending'",
            (status, txid, int(time.time()) if status == 'paid' else None, topup_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def txid_already_used(self, currency, txid):
        cur = await self._conn.execute(
            "SELECT 1 FROM topups WHERE currency=? AND txid=?", (currency, txid)
        )
        return await cur.fetchone() is not None

    async def topup_revenue(self):
        cur = await self._conn.execute(
            "SELECT currency, COUNT(*) AS n, SUM(usd) AS usd, SUM(amount_units) AS units "
            "FROM topups WHERE status='paid' GROUP BY currency"
        )
        return await cur.fetchall()

    async def recent_topups(self, limit=10):
        cur = await self._conn.execute(
            "SELECT * FROM topups WHERE status='paid' ORDER BY paid_at DESC LIMIT ?",
            (limit,),
        )
        return await cur.fetchall()

    # ----- wallet settings (payout addresses / auto-withdraw) -----
    async def get_wallet_settings(self):
        cur = await self._conn.execute("SELECT * FROM wallet_settings")
        return {row["currency"]: dict(row) for row in await cur.fetchall()}

    async def set_payout_address(self, currency, address):
        await self._conn.execute(
            "INSERT INTO wallet_settings (currency, payout_address) VALUES (?,?) "
            "ON CONFLICT(currency) DO UPDATE SET payout_address=excluded.payout_address",
            (currency, address),
        )
        await self._conn.commit()

    async def set_auto_threshold(self, currency, usd):
        await self._conn.execute(
            "INSERT INTO wallet_settings (currency, auto_threshold_usd) VALUES (?,?) "
            "ON CONFLICT(currency) DO UPDATE SET auto_threshold_usd=excluded.auto_threshold_usd",
            (currency, usd),
        )
        await self._conn.commit()

    async def leaderboard(self, guild_id, limit=10):
        cur = await self._conn.execute(
            "SELECT user_id, coins FROM users WHERE guild_id=? "
            "ORDER BY coins DESC, total_eligible_seconds DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()
