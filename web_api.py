"""Authenticated HTTP API exposing coin balances (ported from status-coin-bot).

The bot is the single source of truth for coin balances. The shop bot and the
casino site call these endpoints server-side with the shared API key.

Endpoints (all scoped to the configured guild):
  GET  /health                       -> {"ok": true}
  GET  /api/coins/balance?user_id=   -> {"user_id", "coins"}
  GET  /api/coins/leaderboard?limit= -> {"leaderboard": [{"user_id", "coins"}, ...]}
  GET  /api/coins/profile?user_id=   -> balance + reward progress
  GET  /api/coins/check?user_id=     -> live earning-eligibility breakdown
  GET  /api/coins/settings           -> guild reward settings
  GET  /api/coins/store              -> store tiers priced in coins
  POST /api/coins/settings           -> update one setting {"key", "value"}
  POST /api/coins/pay                -> {"from_id", "to_id", "amount"}
  POST /api/coins/set                -> {"user_id", "amount"}
  POST /api/coins/adjust             -> {"user_id", "delta"}

All /api/* routes require the header `X-API-Key: <CASINO_API_KEY>`.
"""
import hmac
import logging
import time

from aiohttp import web

import config
from coins import get_custom_status_text, parse_statuses

log = logging.getLogger("reseller-bot.web")

_STATUS_ALIASES = {"online", "dnd", "idle", "offline"}


def _clean_status_names(raw):
    out = {p.strip().lower() for p in (raw or "").split(",") if p.strip().lower() in _STATUS_ALIASES}
    return out or {"online", "dnd"}


def _unauthorized():
    return web.json_response({"error": "unauthorized"}, status=401)


def _require_key(request):
    provided = request.headers.get("X-API-Key", "")
    expected = config.CASINO_API_KEY
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _parse_user_id(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


class CoinApi:
    """Coin API route handlers, mounted on the bot's shared web server."""

    def __init__(self, bot):
        self.bot = bot

    @property
    def guild_id(self):
        return config.CASINO_API_GUILD_ID

    @property
    def enabled(self):
        if not config.CASINO_API_KEY:
            log.info("CASINO_API_KEY not set - coin API disabled.")
            return False
        if not self.guild_id:
            log.warning("CASINO_API_KEY set but no GUILD_ID/CASINO_API_GUILD_ID; coin API disabled.")
            return False
        return True

    @property
    def cog(self):
        return self.bot.get_cog("CoinsCog")

    def attach(self, app):
        app.add_routes(
            [
                web.get("/health", self.handle_health),
                web.get("/api/coins/balance", self.handle_balance),
                web.get("/api/coins/leaderboard", self.handle_leaderboard),
                web.get("/api/coins/profile", self.handle_profile),
                web.get("/api/coins/check", self.handle_check),
                web.get("/api/coins/settings", self.handle_get_settings),
                web.get("/api/coins/store", self.handle_store),
                web.post("/api/coins/settings", self.handle_update_setting),
                web.post("/api/coins/pay", self.handle_pay),
                web.post("/api/coins/set", self.handle_set),
                web.post("/api/coins/adjust", self.handle_adjust),
            ]
        )

    # ----- handlers -----
    async def handle_health(self, request):
        return web.json_response({"ok": True})

    async def handle_balance(self, request):
        if not _require_key(request):
            return _unauthorized()
        user_id = _parse_user_id(request.query.get("user_id"))
        if user_id is None:
            return web.json_response({"error": "invalid user_id"}, status=400)
        user = await self.bot.db.get_user(self.guild_id, user_id)
        return web.json_response({"user_id": user_id, "coins": round(user["coins"], 3)})

    async def handle_leaderboard(self, request):
        if not _require_key(request):
            return _unauthorized()
        try:
            limit = int(request.query.get("limit", 10))
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid limit"}, status=400)
        limit = max(1, min(limit, 50))
        rows = await self.bot.db.leaderboard(self.guild_id, limit)
        return web.json_response(
            {"leaderboard": [{"user_id": row["user_id"], "coins": round(row["coins"], 3)} for row in rows]}
        )

    async def handle_profile(self, request):
        if not _require_key(request):
            return _unauthorized()
        user_id = _parse_user_id(request.query.get("user_id"))
        if user_id is None:
            return web.json_response({"error": "invalid user_id"}, status=400)
        settings = await self.cog.get_guild_settings(self.guild_id)
        u = await self.bot.db.get_user(self.guild_id, user_id)
        total = u["total_eligible_seconds"]
        since = self.cog.eligible_since.get((self.guild_id, user_id))
        if since:
            total += time.time() - since
        reward_seconds = settings["reward_seconds"]
        into = total % reward_seconds if reward_seconds else 0
        remaining = (reward_seconds - into) if reward_seconds else 0
        pct = (into / reward_seconds * 100) if reward_seconds else 0
        return web.json_response(
            {
                "user_id": user_id,
                "coins": round(u["coins"], 3),
                "rewards_count": u["rewards_count"],
                "total_seconds": total,
                "remaining_seconds": remaining,
                "percent": pct,
            }
        )

    async def handle_check(self, request):
        if not _require_key(request):
            return _unauthorized()
        user_id = _parse_user_id(request.query.get("user_id"))
        if user_id is None:
            return web.json_response({"error": "invalid user_id"}, status=400)
        settings = await self.cog.get_guild_settings(self.guild_id)
        guild = self.bot.get_guild(self.guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is None:
            return web.json_response(
                {
                    "found": False,
                    "required_status": settings["required_status"],
                    "eligible_statuses": settings["eligible_statuses"],
                }
            )
        status_ok = member.status in parse_statuses(settings["eligible_statuses"])
        text = get_custom_status_text(member)
        text_ok = settings["required_status"].strip().lower() in (text or "").lower()
        return web.json_response(
            {
                "found": True,
                "status": str(member.status),
                "status_text": text,
                "status_ok": status_ok,
                "text_ok": text_ok,
                "eligible": status_ok and text_ok and not member.bot,
                "required_status": settings["required_status"],
                "eligible_statuses": settings["eligible_statuses"],
            }
        )

    async def handle_get_settings(self, request):
        if not _require_key(request):
            return _unauthorized()
        s = await self.cog.get_guild_settings(self.guild_id)
        return web.json_response(
            {
                "required_status": s["required_status"],
                "reward_hours": s["reward_seconds"] / 3600.0,
                "coins_per_reward": s["coins_per_reward"],
                "eligible_statuses": s["eligible_statuses"],
                "coin_name": config.COIN_NAME,
                "coin_emoji": config.COIN_EMOJI,
            }
        )

    async def handle_store(self, request):
        if not _require_key(request):
            return _unauthorized()
        return web.json_response(
            {
                "tiers": [
                    {
                        "account_type": t["account_type"],
                        "label": t.get("label", t["account_type"]),
                        "cost": int(t.get("cost", 0)),
                    }
                    for t in config.STORE_TIERS
                ],
                "coin_name": config.COIN_NAME,
                "coin_emoji": config.COIN_EMOJI,
            }
        )

    async def handle_update_setting(self, request):
        if not _require_key(request):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        key = body.get("key")
        value = body.get("value")
        if key == "required_status":
            if not isinstance(value, str) or not value.strip():
                return web.json_response({"error": "value must be a non-empty string"}, status=400)
            await self.bot.db.update_coin_setting(self.guild_id, "required_status", value.strip())
        elif key == "reward_hours":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                return web.json_response({"error": "value must be a positive number"}, status=400)
            await self.bot.db.update_coin_setting(self.guild_id, "reward_seconds", float(value) * 3600.0)
        elif key == "coins_per_reward":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                return web.json_response({"error": "value must be a positive integer"}, status=400)
            await self.bot.db.update_coin_setting(self.guild_id, "coins_per_reward", value)
        elif key == "eligible_statuses":
            if not isinstance(value, str):
                return web.json_response({"error": "value must be a string"}, status=400)
            cleaned = ",".join(sorted(_clean_status_names(value)))
            await self.bot.db.update_coin_setting(self.guild_id, "eligible_statuses", cleaned)
        else:
            return web.json_response({"error": "unknown setting key"}, status=400)
        self.cog.invalidate_settings(self.guild_id)
        s = await self.cog.get_guild_settings(self.guild_id)
        return web.json_response(
            {
                "ok": True,
                "settings": {
                    "required_status": s["required_status"],
                    "reward_hours": s["reward_seconds"] / 3600.0,
                    "coins_per_reward": s["coins_per_reward"],
                    "eligible_statuses": s["eligible_statuses"],
                },
            }
        )

    async def handle_pay(self, request):
        if not _require_key(request):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        from_id = _parse_user_id(body.get("from_id"))
        to_id = _parse_user_id(body.get("to_id"))
        amount = body.get("amount")
        if from_id is None or to_id is None:
            return web.json_response({"error": "invalid user ids"}, status=400)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return web.json_response({"error": "amount must be a positive integer"}, status=400)
        if from_id == to_id:
            return web.json_response({"error": "cannot pay yourself"}, status=400)
        ok, sender_balance = await self.bot.db.try_adjust_coins(self.guild_id, from_id, -amount)
        if not ok:
            return web.json_response({"error": "insufficient_balance", "coins": round(sender_balance, 3)}, status=409)
        recipient_balance = await self.bot.db.add_coins(self.guild_id, to_id, amount)
        return web.json_response(
            {
                "ok": True,
                "from_id": from_id,
                "to_id": to_id,
                "amount": amount,
                "from_coins": round(sender_balance, 3),
                "to_coins": round(recipient_balance, 3),
            }
        )

    async def handle_set(self, request):
        if not _require_key(request):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        user_id = _parse_user_id(body.get("user_id"))
        amount = body.get("amount")
        if user_id is None:
            return web.json_response({"error": "invalid user_id"}, status=400)
        if not isinstance(amount, int) or isinstance(amount, bool):
            return web.json_response({"error": "amount must be an integer"}, status=400)
        new = await self.bot.db.set_coins(self.guild_id, user_id, amount)
        return web.json_response({"user_id": user_id, "coins": round(new, 3)})

    async def handle_adjust(self, request):
        if not _require_key(request):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        user_id = _parse_user_id(body.get("user_id"))
        if user_id is None:
            return web.json_response({"error": "invalid user_id"}, status=400)
        delta = body.get("delta")
        if not isinstance(delta, int) or isinstance(delta, bool):
            return web.json_response({"error": "delta must be an integer"}, status=400)
        ok, balance = await self.bot.db.try_adjust_coins(self.guild_id, user_id, delta)
        if not ok:
            return web.json_response({"error": "insufficient_balance", "coins": round(balance, 3)}, status=409)
        return web.json_response({"user_id": user_id, "coins": round(balance, 3), "applied": delta})
