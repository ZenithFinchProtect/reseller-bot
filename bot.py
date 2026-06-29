"""Nordic reseller Discord bot.

Gives resellers self-serve tools against the NFA API:
  /stock         - live stock snapshot (counts capped, default 5)
  /stock-check   - register THIS server's Discord webhook for recurring stock updates
  /stock-stop    - stop the recurring updates
  /check <key>   - re-validate an activated key
  /replace <key> - replace an invalid key within the 3-hour warranty
  /delete <key>  - delete an unactivated key
  /buy           - placeholder (balance + checkout arrive with the website)
"""
import logging
import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from db import Database

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("reseller-bot")

intents = discord.Intents.default()
intents.guilds = True

WEBHOOK_RE = re.compile(
    r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$"
)

# Friendly group names by raw-key prefix, in display order.
GAME_GROUPS = [
    ("cs2", "CS2"),
    ("rust", "Rust"),
    ("arc", "Arc Raiders"),
    ("battlefield", "Battlefield 6"),
    ("dayz", "DayZ"),
    ("escape_from_tarkov", "Escape from Tarkov"),
    ("eft", "Escape from Tarkov"),
]

_LABEL_FIXES = {
    "cs2": "", "rust": "", "arc": "", "dayz": "", "eft": "",
    "hours": "h", "hour": "h", "inv": "INV", "inactive": "inactive",
    "elo": "ELO", "medals": "medals", "premium": "Premium",
    "premier": "Premier", "prime": "Prime", "plus": "+", "last": "last",
    "season": "season", "battlefield": "Battlefield", "escape": "Escape",
    "from": "from", "tarkov": "Tarkov", "random": "Random",
}


def display_count(count, cap):
    try:
        count = int(count)
    except (TypeError, ValueError):
        return 0
    if cap and cap > 0 and count > cap:
        return cap
    return max(0, count)


def group_for(key):
    for prefix, name in GAME_GROUPS:
        if key.startswith(prefix):
            return name
    return "Other"


def prettify(key):
    """Turn a raw NFA key like 'rust_3000_7000_hours' into 'Rust 3000-7000 h'."""
    parts = key.split("_")
    out = []
    for p in parts:
        if p in _LABEL_FIXES:
            mapped = _LABEL_FIXES[p]
            if mapped:
                out.append(mapped)
        elif p.isdigit():
            out.append(p)
        else:
            out.append(p.capitalize())
    label = " ".join(out).strip()
    # "3000 7000 h" -> "3000-7000 h"
    label = re.sub(r"(\d+)\s+(\d+)", r"\1-\2", label)
    return label or key


def build_stock_embed(stock, cap):
    grouped = {}
    for key, value in sorted(stock.items()):
        n = display_count(value, cap)
        grouped.setdefault(group_for(key), []).append((prettify(key), n))

    embed = discord.Embed(title=config.STOCK_EMBED_TITLE, color=config.EMBED_COLOR)
    order = [name for _, name in GAME_GROUPS] + ["Other"]
    seen = set()
    for name in order:
        if name in seen or name not in grouped:
            continue
        seen.add(name)
        lines = [f"{label}: **{n}**" for label, n in grouped[name]]
        embed.add_field(name=name, value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="Live stock \u00b7 nordicnfas.com")
    embed.timestamp = discord.utils.utcnow()
    return embed


# --------------------------- bot ---------------------------
class ResellerBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(config.DB_PATH)
        self.session = None

    async def setup_hook(self):
        await self.db.connect()
        self.session = aiohttp.ClientSession()
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", config.GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (up to ~1h first time)")
        self.stock_update_loop.start()

    async def close(self):
        if self.session is not None:
            await self.session.close()
        await self.db.close()
        await super().close()

    # ----- NFA API helpers -----
    async def nfa_get(self, path, params=None):
        url = f"{config.NFA_API_BASE}{path}"
        headers = {"X-API-Key": config.NFA_API_KEY}
        timeout = aiohttp.ClientTimeout(total=20)
        async with self.session.get(
            url, params=params, headers=headers, timeout=timeout
        ) as resp:
            return resp.status, await resp.json(content_type=None)

    async def nfa_post(self, path, payload, timeout=30):
        url = f"{config.NFA_API_BASE}{path}"
        headers = {"X-API-Key": config.NFA_API_KEY}
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with self.session.post(
            url, json=payload, headers=headers, timeout=client_timeout
        ) as resp:
            return resp.status, await resp.json(content_type=None)

    async def nfa_delete(self, path, payload, timeout=30):
        url = f"{config.NFA_API_BASE}{path}"
        headers = {"X-API-Key": config.NFA_API_KEY}
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with self.session.delete(
            url, json=payload, headers=headers, timeout=client_timeout
        ) as resp:
            return resp.status, await resp.json(content_type=None)

    async def fetch_stock(self):
        _, data = await self.nfa_get("/api/v1/stock")
        return data.get("stock", {}) if isinstance(data, dict) else {}

    async def post_to_webhook(self, url, embed):
        payload = {
            "embeds": [embed.to_dict()],
            "username": config.WEBHOOK_USERNAME,
        }
        if config.WEBHOOK_AVATAR_URL:
            payload["avatar_url"] = config.WEBHOOK_AVATAR_URL
        async with self.session.post(url, json=payload) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"webhook {resp.status}: {text[:200]}")

    # ----- recurring stock updates -----
    @tasks.loop(minutes=config.STOCK_UPDATE_MINUTES)
    async def stock_update_loop(self):
        subs = await self.db.all_subscriptions()
        if not subs:
            return
        try:
            stock = await self.fetch_stock()
        except Exception as exc:  # noqa: BLE001
            log.warning("stock fetch failed: %s", exc)
            return
        embed = build_stock_embed(stock, config.STOCK_CAP)
        for row in subs:
            try:
                await self.post_to_webhook(row["webhook_url"], embed)
            except Exception as exc:  # noqa: BLE001
                log.warning("post to guild %s failed: %s", row["guild_id"], exc)

    @stock_update_loop.before_loop
    async def _before_loop(self):
        await self.wait_until_ready()


bot = ResellerBot()


def _need_api_key(interaction):
    return not config.NFA_API_KEY


# --------------------------- commands ---------------------------
@bot.tree.command(name="stock", description="Show live account stock (capped)")
@app_commands.guild_only()
async def stock(interaction: discord.Interaction):
    if _need_api_key(interaction):
        await interaction.response.send_message(
            "Stock isn't configured yet (an admin must set `NFA_API_KEY`).",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    try:
        data = await bot.fetch_stock()
    except Exception as exc:  # noqa: BLE001
        log.warning("/stock fetch failed: %s", exc)
        await interaction.followup.send(
            "Couldn't fetch stock right now \u2014 please try again shortly."
        )
        return
    await interaction.followup.send(embed=build_stock_embed(data, config.STOCK_CAP))


@bot.tree.command(
    name="stock-check",
    description="Register this server's webhook to receive recurring stock updates",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    webhook_url="A Discord webhook URL from this server (Server Settings > Integrations > Webhooks)"
)
async def stock_check(interaction: discord.Interaction, webhook_url: str):
    webhook_url = webhook_url.strip()
    if not WEBHOOK_RE.match(webhook_url):
        await interaction.response.send_message(
            "That doesn't look like a Discord webhook URL. Create one in "
            "**Server Settings \u2192 Integrations \u2192 Webhooks**, copy its URL, "
            "and run this again.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    # Validate by sending the first stock update to the webhook.
    try:
        stock_data = await bot.fetch_stock()
        embed = build_stock_embed(stock_data, config.STOCK_CAP)
        await bot.post_to_webhook(webhook_url, embed)
    except Exception as exc:  # noqa: BLE001
        log.warning("/stock-check validation failed: %s", exc)
        await interaction.followup.send(
            "Couldn't post to that webhook (is the URL correct and the channel "
            "still there?). Nothing was saved.",
            ephemeral=True,
        )
        return

    await bot.db.set_subscription(
        interaction.guild_id, webhook_url, interaction.user.id
    )
    await interaction.followup.send(
        f"\u2705 Done! This server will now get stock updates every "
        f"**{config.STOCK_UPDATE_MINUTES} min** (a first update was just posted). "
        f"Use `/stock-stop` to turn it off.",
        ephemeral=True,
    )


@bot.tree.command(
    name="stock-stop", description="Stop recurring stock updates for this server"
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def stock_stop(interaction: discord.Interaction):
    removed = await bot.db.remove_subscription(interaction.guild_id)
    msg = (
        "Recurring stock updates have been turned off for this server."
        if removed
        else "This server wasn't receiving stock updates."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="check", description="Re-validate an activated account key")
@app_commands.guild_only()
@app_commands.describe(key="The activation key to check")
async def check(interaction: discord.Interaction, key: str):
    if _need_api_key(interaction):
        await interaction.response.send_message(
            "Not configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        _, data = await bot.nfa_post(
            "/api/v1/check_account", {"activation_key": key.strip()}
        )
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Error: {exc}", ephemeral=True)
        return
    result = data.get("result") if isinstance(data, dict) else None
    if result == "valid":
        await interaction.followup.send("\u2705 Account is **valid**.", ephemeral=True)
    elif result == "replaced":
        rep = data.get("replacement_key")
        body = "\U0001F504 Account was **replaced** under the 3-hour warranty."
        if rep:
            body += f"\n\n**New key:** ||`{rep}`||"
        await interaction.followup.send(body, ephemeral=True)
    else:
        msg = (data.get("message") if isinstance(data, dict) else None) or "invalid"
        await interaction.followup.send(
            f"\u274C Account is **invalid** ({msg}).", ephemeral=True
        )


@bot.tree.command(
    name="replace",
    description="Replace an invalid key within the 3-hour warranty",
)
@app_commands.guild_only()
@app_commands.describe(key="The activation key to replace")
async def replace(interaction: discord.Interaction, key: str):
    if _need_api_key(interaction):
        await interaction.response.send_message(
            "Not configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        _, data = await bot.nfa_post(
            "/api/v1/check_account", {"activation_key": key.strip()}
        )
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(
            "Couldn't reach the replacement service \u2014 try again shortly.",
            ephemeral=True,
        )
        return
    result = data.get("result") if isinstance(data, dict) else None
    if result == "replaced":
        rep = data.get("replacement_key")
        body = "\U0001F504 Replaced under the 3-hour warranty."
        if rep:
            body += (
                f"\n\n**New key:** ||`{rep}`||\n\n"
                f"Activate it at {config.ACTIVATION_URL}"
            )
        await interaction.followup.send(body, ephemeral=True)
    elif result == "valid":
        await interaction.followup.send(
            "\u2705 That account is still **valid** \u2014 no replacement needed.",
            ephemeral=True,
        )
    else:
        msg = (data.get("message") if isinstance(data, dict) else None) or (
            "outside the 3-hour warranty window"
        )
        await interaction.followup.send(
            f"\u274C No replacement issued \u2014 {msg}.", ephemeral=True
        )


@bot.tree.command(
    name="delete", description="Delete an unactivated key (removes it from stock)"
)
@app_commands.guild_only()
@app_commands.describe(key="The unactivated activation key to delete")
async def delete(interaction: discord.Interaction, key: str):
    if _need_api_key(interaction):
        await interaction.response.send_message(
            "Not configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        status, data = await bot.nfa_delete(
            "/api/v1/unactivated_keys", {"activation_key": key.strip()}
        )
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Error: {exc}", ephemeral=True)
        return
    ok = status < 400 and (
        not isinstance(data, dict) or data.get("status") != "error"
    )
    if ok:
        await interaction.followup.send("\U0001F5D1\uFE0F Key deleted.", ephemeral=True)
    else:
        msg = (data.get("message") if isinstance(data, dict) else None) or (
            "could not delete (only unactivated keys can be deleted)"
        )
        await interaction.followup.send(f"\u274C {msg}.", ephemeral=True)


@bot.tree.command(
    name="buy", description="Purchase keys (coming soon \u2014 balance via the website)"
)
@app_commands.guild_only()
async def buy(interaction: discord.Interaction):
    await interaction.response.send_message(
        "\U0001F6D2 Purchasing isn't live yet. Balances and checkout are coming "
        "with the reseller website \u2014 you'll be able to top up there and buy here.",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need the **Manage Server** permission to do that."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "You can't use that command here."
    else:
        log.exception("command error: %s", error)
        msg = "Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        await bot.change_presence(
            activity=discord.CustomActivity(name=config.BOT_STATUS_TEXT)
        )
    except Exception:  # noqa: BLE001
        pass


def main():
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Put it in .env or the environment.")
    bot.run(config.TOKEN)


if __name__ == "__main__":
    main()
