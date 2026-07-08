"""Nordic reseller Discord bot.

Self-serve tools for resellers against the NFA API, plus per-server stock
update webhooks the bot manages for you.

  /stock              - live stock snapshot (capped)
  /test               - health check (bot, NFA API, this server's webhook)
  /stock-url <url>    - paste a Discord webhook URL; updates post there
  /webhook-settings    - choose which games show, the cap, hide out-of-stock,
                        and how often updates send
  /stock-stop         - stop updates for this server
  /check <key>        - re-validate an activated key
  /replace <key>      - replace an invalid key within the 3-hour warranty
  /delete <key>       - delete an unactivated key
  /buy                - placeholder (balance + checkout arrive with the website)
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
ALL_GAMES = ["CS2", "Rust", "Arc Raiders", "Battlefield 6", "DayZ", "Escape from Tarkov"]
INTERVAL_CHOICES = [5, 15, 30, 60, 120, 360, 720]
CAP_CHOICES = [("No cap", 0), ("3", 3), ("5", 5), ("10", 10), ("25", 25)]

_LABEL_FIXES = {
    "cs2": "", "rust": "", "arc": "", "dayz": "", "eft": "",
    "hours": "h", "hour": "h", "inv": "INV", "inactive": "inactive",
    "elo": "ELO", "medals": "medals", "premium": "Premium",
    "premier": "Premier", "prime": "Prime", "plus": "+", "last": "last",
    "season": "season", "battlefield": "Battlefield", "escape": "Escape",
    "from": "from", "tarkov": "Tarkov", "random": "Random",
}


# --------------------------- formatting helpers ---------------------------
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
    out = []
    for p in key.split("_"):
        if p in _LABEL_FIXES:
            mapped = _LABEL_FIXES[p]
            if mapped:
                out.append(mapped)
        elif p.isdigit():
            out.append(p)
        else:
            out.append(p.capitalize())
    label = " ".join(out).strip()
    label = re.sub(r"(\d+)\s+(\d+)", r"\1-\2", label)
    return label or key


def parse_games(raw):
    """Stored csv -> list of game names, or None (= all)."""
    if not raw:
        return None
    games = [g.strip() for g in raw.split(",") if g.strip()]
    return games or None


def build_stock_embed(stock, *, cap, games=None, show_zero=True, title=None):
    include = set(games) if games else None
    grouped = {}
    for key, value in sorted(stock.items()):
        gname = group_for(key)
        if include is not None and gname not in include:
            continue
        n = display_count(value, cap)
        if not show_zero and n == 0:
            continue
        grouped.setdefault(gname, []).append((prettify(key), n))

    embed = discord.Embed(
        title=title or config.STOCK_EMBED_TITLE, color=config.EMBED_COLOR
    )
    order = [name for _, name in GAME_GROUPS] + ["Other"]
    seen = set()
    for name in order:
        if name in seen or name not in grouped:
            continue
        seen.add(name)
        lines = [f"{label}: **{n}**" for label, n in grouped[name]]
        embed.add_field(name=name, value="\n".join(lines)[:1024], inline=False)
    if not embed.fields:
        embed.description = "No products to show with the current settings."
    embed.set_footer(text="Live stock \u00b7 nordicnfas.com")
    embed.timestamp = discord.utils.utcnow()
    return embed


def embed_for_sub(stock, sub):
    """Build the embed using a subscription row's saved settings."""
    cap = sub["cap"] if sub["cap"] is not None else config.STOCK_CAP
    return build_stock_embed(
        stock,
        cap=cap,
        games=parse_games(sub["games"]),
        show_zero=bool(sub["show_zero"]),
        title=sub["title"],
    )


def sub_interval(sub):
    val = sub["interval_minutes"]
    return val if val and val > 0 else config.STOCK_UPDATE_MINUTES


# --------------------------- bot ---------------------------
class ResellerBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(config.DB_PATH)
        self.session = None
        self._stock_cache = None

    async def setup_hook(self):
        await self.db.connect()
        self.session = aiohttp.ClientSession()
        # Always keep the commands registered globally (so the bot can handle
        # interactions in any scope). If GUILD_ID is set, also register them to
        # that guild for instant availability.
        synced = await self.tree.sync()
        log.info("Synced %d command(s) globally", len(synced))
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            gsynced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s", len(gsynced), config.GUILD_ID)
        self.stock_tick.start()

    async def close(self):
        if self.session is not None:
            await self.session.close()
        await self.db.close()
        await super().close()

    # ----- NFA API helpers -----
    async def _nfa(self, method, path, *, params=None, json=None, timeout=30):
        url = f"{config.NFA_API_BASE}{path}"
        headers = {"X-API-Key": config.NFA_API_KEY}
        ct = aiohttp.ClientTimeout(total=timeout)
        async with self.session.request(
            method, url, params=params, json=json, headers=headers, timeout=ct
        ) as resp:
            return resp.status, await resp.json(content_type=None)

    async def fetch_stock(self):
        """Live stock map, cached 2 min to protect the NFA rate limit."""
        import time

        now = time.time()
        if self._stock_cache and now - self._stock_cache[0] < 120:
            return self._stock_cache[1]
        _, data = await self._nfa("GET", "/api/v1/stock", timeout=20)
        stock = data.get("stock", {}) if isinstance(data, dict) else {}
        self._stock_cache = (now, stock)
        return stock

    async def post_to_webhook(self, url, embed):
        payload = {"embeds": [embed.to_dict()], "username": config.WEBHOOK_USERNAME}
        if config.WEBHOOK_AVATAR_URL:
            payload["avatar_url"] = config.WEBHOOK_AVATAR_URL
        async with self.session.post(url, json=payload) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"webhook {resp.status}: {text[:200]}")

    # ----- recurring stock updates (per-guild interval) -----
    @tasks.loop(seconds=60)
    async def stock_tick(self):
        import time

        subs = await self.db.all_subscriptions()
        if not subs:
            return
        due = [s for s in subs if time.time() - (s["last_sent_at"] or 0) >= sub_interval(s) * 60]
        if not due:
            return
        try:
            stock = await self.fetch_stock()
        except Exception as exc:  # noqa: BLE001
            log.warning("stock fetch failed: %s", exc)
            return
        for sub in due:
            try:
                await self.post_to_webhook(sub["webhook_url"], embed_for_sub(stock, sub))
                await self.db.mark_sent(sub["guild_id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("post to guild %s failed: %s", sub["guild_id"], exc)
                # Avoid hammering a dead webhook every tick.
                await self.db.mark_sent(sub["guild_id"])

    @stock_tick.before_loop
    async def _before(self):
        await self.wait_until_ready()


bot = ResellerBot()


def _no_key():
    return not config.NFA_API_KEY


# --------------------------- settings UI ---------------------------
class SettingsView(discord.ui.View):
    def __init__(self, owner_id, sub):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        games = parse_games(sub["games"]) or ALL_GAMES
        cap = sub["cap"] if sub["cap"] is not None else config.STOCK_CAP
        interval = sub_interval(sub)
        show_zero = bool(sub["show_zero"])

        self.games_select = discord.ui.Select(
            placeholder="Games to show",
            min_values=1,
            max_values=len(ALL_GAMES),
            options=[
                discord.SelectOption(label=g, value=g, default=(g in games))
                for g in ALL_GAMES
            ],
        )
        self.games_select.callback = self._on_games
        self.add_item(self.games_select)

        self.interval_select = discord.ui.Select(
            placeholder="How often updates send",
            options=[
                discord.SelectOption(
                    label=(f"{m} min" if m < 60 else f"{m // 60} h"),
                    value=str(m),
                    default=(m == interval),
                )
                for m in INTERVAL_CHOICES
            ],
        )
        self.interval_select.callback = self._on_interval
        self.add_item(self.interval_select)

        self.cap_select = discord.ui.Select(
            placeholder="Max count shown (cap)",
            options=[
                discord.SelectOption(
                    label=lbl, value=str(val), default=(val == cap)
                )
                for lbl, val in CAP_CHOICES
            ],
        )
        self.cap_select.callback = self._on_cap
        self.add_item(self.cap_select)

        self.zero_button = discord.ui.Button(
            label=("Out-of-stock: shown" if show_zero else "Out-of-stock: hidden"),
            style=(discord.ButtonStyle.secondary if show_zero else discord.ButtonStyle.primary),
        )
        self.zero_button.callback = self._on_zero
        self.add_item(self.zero_button)

        self.test_button = discord.ui.Button(
            label="Send update now", style=discord.ButtonStyle.success
        )
        self.test_button.callback = self._on_test
        self.add_item(self.test_button)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "These controls aren't yours.", ephemeral=True
            )
            return False
        return True

    async def _refresh(self, interaction, note):
        sub = await bot.db.get_subscription(interaction.guild_id)
        await interaction.response.edit_message(embed=settings_embed(sub, note), view=self)

    async def _on_games(self, interaction):
        await bot.db.update_settings(
            interaction.guild_id, games=",".join(self.games_select.values)
        )
        for opt in self.games_select.options:
            opt.default = opt.value in self.games_select.values
        await self._refresh(interaction, "Updated which games show.")

    async def _on_interval(self, interaction):
        minutes = int(self.interval_select.values[0])
        await bot.db.update_settings(interaction.guild_id, interval_minutes=minutes)
        for opt in self.interval_select.options:
            opt.default = opt.value == self.interval_select.values[0]
        await self._refresh(interaction, "Updated how often updates send.")

    async def _on_cap(self, interaction):
        cap = int(self.cap_select.values[0])
        await bot.db.update_settings(interaction.guild_id, cap=cap)
        for opt in self.cap_select.options:
            opt.default = opt.value == self.cap_select.values[0]
        await self._refresh(interaction, "Updated the display cap.")

    async def _on_zero(self, interaction):
        sub = await bot.db.get_subscription(interaction.guild_id)
        new_val = 0 if bool(sub["show_zero"]) else 1
        await bot.db.update_settings(interaction.guild_id, show_zero=new_val)
        self.zero_button.label = (
            "Out-of-stock: shown" if new_val else "Out-of-stock: hidden"
        )
        self.zero_button.style = (
            discord.ButtonStyle.secondary if new_val else discord.ButtonStyle.primary
        )
        await self._refresh(
            interaction,
            "Out-of-stock rows will now be shown." if new_val else "Out-of-stock rows are now hidden.",
        )

    async def _on_test(self, interaction):
        await interaction.response.defer()
        sub = await bot.db.get_subscription(interaction.guild_id)
        try:
            stock = await bot.fetch_stock()
            await bot.post_to_webhook(sub["webhook_url"], embed_for_sub(stock, sub))
            await bot.db.mark_sent(interaction.guild_id)
            note = "Sent a stock update to your channel."
        except Exception as exc:  # noqa: BLE001
            note = f"Couldn't send: {exc}"
        sub = await bot.db.get_subscription(interaction.guild_id)
        await interaction.edit_original_response(embed=settings_embed(sub, note), view=self)


def settings_embed(sub, note=None):
    games = parse_games(sub["games"])
    cap = sub["cap"] if sub["cap"] is not None else config.STOCK_CAP
    embed = discord.Embed(title="Stock webhook settings", color=config.EMBED_COLOR)
    embed.add_field(name="Games", value=", ".join(games) if games else "All", inline=False)
    embed.add_field(name="Sends every", value=f"{sub_interval(sub)} min", inline=True)
    embed.add_field(name="Cap", value=("No cap" if not cap else str(cap)), inline=True)
    embed.add_field(
        name="Out-of-stock", value=("Shown" if bool(sub["show_zero"]) else "Hidden"), inline=True
    )
    if note:
        embed.description = note
    return embed


# --------------------------- commands ---------------------------
@bot.tree.command(name="stock", description="Show live account stock (capped)")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 120.0, key=lambda i: i.user.id)
async def stock(interaction: discord.Interaction):
    if _no_key():
        await interaction.response.send_message(
            "Stock isn't configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    await interaction.response.defer(thinking=True)
    try:
        data = await bot.fetch_stock()
    except Exception:  # noqa: BLE001
        await interaction.followup.send("Couldn't fetch stock right now \u2014 try again shortly.")
        return
    await interaction.followup.send(
        embed=build_stock_embed(data, cap=config.STOCK_CAP)
    )


@bot.tree.command(
    name="stock-url",
    description="Paste a Discord webhook URL and stock updates will post there",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    webhook_url="Your Discord webhook URL (Channel/Server Settings > Integrations > Webhooks > Copy URL)"
)
async def stock_url(interaction: discord.Interaction, webhook_url: str):
    if _no_key():
        await interaction.response.send_message(
            "Not configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    webhook_url = webhook_url.strip()
    if not WEBHOOK_RE.match(webhook_url):
        await interaction.response.send_message(
            "That doesn't look like a Discord webhook URL. In Discord open "
            "**Server Settings \u2192 Integrations \u2192 Webhooks**, create one for the "
            "channel you want, click **Copy Webhook URL**, and paste it here.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    existing = await bot.db.get_subscription(interaction.guild_id)
    try:
        await bot.db.set_subscription(interaction.guild_id, webhook_url, interaction.user.id)
        stock_data = await bot.fetch_stock()
        sub = await bot.db.get_subscription(interaction.guild_id)
        await bot.post_to_webhook(webhook_url, embed_for_sub(stock_data, sub))
        await bot.db.mark_sent(interaction.guild_id)
    except Exception:  # noqa: BLE001
        # Roll back so a bad URL doesn't leave a broken subscription.
        if existing is None:
            await bot.db.remove_subscription(interaction.guild_id)
        await interaction.followup.send(
            "Couldn't post to that webhook (is the URL correct and the channel "
            "still there?). Nothing was saved.",
            ephemeral=True,
        )
        return
    note = " (replaced the previous webhook for this server)" if existing else ""
    await interaction.followup.send(
        f"\u2705 Done! A first update was just posted, and stock updates will send "
        f"every **{sub_interval(sub)} min**{note}. Use `/webhook-settings` to choose "
        f"what shows and how often, or `/stock-stop` to turn it off.",
        ephemeral=True,
    )


@bot.tree.command(
    name="webhook-settings",
    description="Customise what the stock updates show and how often they send",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def webhook_settings(interaction: discord.Interaction):
    sub = await bot.db.get_subscription(interaction.guild_id)
    if sub is None:
        await interaction.response.send_message(
            "This server isn't getting stock updates yet. Paste a webhook with "
            "`/stock-url` first.",
            ephemeral=True,
        )
        return
    view = SettingsView(interaction.user.id, sub)
    await interaction.response.send_message(
        embed=settings_embed(sub), view=view, ephemeral=True
    )


@bot.tree.command(name="stock-stop", description="Stop stock updates for this server")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def stock_stop(interaction: discord.Interaction):
    sub = await bot.db.get_subscription(interaction.guild_id)
    if sub is None:
        await interaction.response.send_message(
            "This server wasn't receiving stock updates.", ephemeral=True
        )
        return
    await bot.db.remove_subscription(interaction.guild_id)
    await interaction.response.send_message(
        "Stock updates turned off for this server. (Your webhook still exists in "
        "Discord — delete it there if you want it gone.)",
        ephemeral=True,
    )


@bot.tree.command(name="check", description="Re-validate an activated account key")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 120.0, key=lambda i: i.user.id)
@app_commands.describe(key="The activation key to check")
async def check(interaction: discord.Interaction, key: str):
    if _no_key():
        await interaction.response.send_message(
            "Not configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        _, data = await bot._nfa("POST", "/api/v1/check_account", json={"activation_key": key.strip()})
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
        await interaction.followup.send(f"\u274C Account is **invalid** ({msg}).", ephemeral=True)


@bot.tree.command(name="replace", description="Replace an invalid key within the 3-hour warranty")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 120.0, key=lambda i: i.user.id)
@app_commands.describe(key="The activation key to replace")
async def replace(interaction: discord.Interaction, key: str):
    if _no_key():
        await interaction.response.send_message(
            "Not configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        _, data = await bot._nfa("POST", "/api/v1/check_account", json={"activation_key": key.strip()})
    except Exception:  # noqa: BLE001
        await interaction.followup.send(
            "Couldn't reach the replacement service \u2014 try again shortly.", ephemeral=True
        )
        return
    result = data.get("result") if isinstance(data, dict) else None
    if result == "replaced":
        rep = data.get("replacement_key")
        body = "\U0001F504 Replaced under the 3-hour warranty."
        if rep:
            body += f"\n\n**New key:** ||`{rep}`||\n\nActivate it at {config.ACTIVATION_URL}"
        await interaction.followup.send(body, ephemeral=True)
    elif result == "valid":
        await interaction.followup.send(
            "\u2705 That account is still **valid** \u2014 no replacement needed.", ephemeral=True
        )
    else:
        msg = (data.get("message") if isinstance(data, dict) else None) or "outside the 3-hour warranty window"
        await interaction.followup.send(f"\u274C No replacement issued \u2014 {msg}.", ephemeral=True)


@bot.tree.command(name="delete", description="Delete an unactivated key (removes it from stock)")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 120.0, key=lambda i: i.user.id)
@app_commands.describe(key="The unactivated activation key to delete")
async def delete(interaction: discord.Interaction, key: str):
    if _no_key():
        await interaction.response.send_message(
            "Not configured yet (an admin must set `NFA_API_KEY`).", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        status, data = await bot._nfa("DELETE", "/api/v1/unactivated_keys", json={"activation_key": key.strip()})
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Error: {exc}", ephemeral=True)
        return
    ok = status < 400 and (not isinstance(data, dict) or data.get("status") != "error")
    if ok:
        await interaction.followup.send("\U0001F5D1\uFE0F Key deleted.", ephemeral=True)
    else:
        msg = (data.get("message") if isinstance(data, dict) else None) or "could not delete (only unactivated keys can be deleted)"
        await interaction.followup.send(f"\u274C {msg}.", ephemeral=True)


@bot.tree.command(name="buy", description="Purchase keys (coming soon \u2014 balance via the website)")
@app_commands.guild_only()
async def buy(interaction: discord.Interaction):
    await interaction.response.send_message(
        "\U0001F6D2 Purchasing isn't live yet. Balances and checkout are coming with the "
        "reseller website \u2014 you'll be able to top up there and buy here.",
        ephemeral=True,
    )


@bot.tree.command(name="test", description="Health check: bot, NFA API, and this server's webhook")
@app_commands.guild_only()
async def test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = [f"\U0001F7E2 Bot online \u2014 latency **{round(bot.latency * 1000)}ms**"]

    # NFA API
    if _no_key():
        lines.append("\u26A0\uFE0F NFA API: `NFA_API_KEY` not set")
    else:
        try:
            stock = await bot.fetch_stock()
            total = sum(int(v) for v in stock.values() if str(v).lstrip("-").isdigit())
            lines.append(f"\u2705 NFA API reachable \u2014 {len(stock)} product(s), {total} total in stock")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\u274C NFA API error: {exc}")

    # This server's webhook
    sub = await bot.db.get_subscription(interaction.guild_id)
    if sub is None:
        lines.append("\u2139\uFE0F No stock webhook set here yet (use `/stock-url`)")
    else:
        try:
            stock = await bot.fetch_stock()
            await bot.post_to_webhook(sub["webhook_url"], embed_for_sub(stock, sub))
            await bot.db.mark_sent(interaction.guild_id)
            lines.append(f"\u2705 Webhook works \u2014 sent a test update (every {sub_interval(sub)} min)")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\u274C Webhook failed: {exc}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"\u23F3 Slow down \u2014 try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "You need the **Manage Server** permission to do that."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "You can't use that command here."
    else:
        log.exception("command error: %s", error)
        cause = getattr(error, "original", error)
        msg = f"Something went wrong running that command: `{type(cause).__name__}: {cause}`"[:1900]
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
        await bot.change_presence(activity=discord.CustomActivity(name=config.BOT_STATUS_TEXT))
    except Exception:  # noqa: BLE001
        pass


def main():
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Put it in .env or the environment.")
    bot.run(config.TOKEN)


if __name__ == "__main__":
    main()
