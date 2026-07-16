"""Coin economy merged in from status-coin-bot, driven by a button hub.

Members earn coins for time spent online (or DND) while displaying the
required custom-status text. Everything coin-related lives behind a single
`/coin` command that opens a button menu:

  Balance / Earn info / Leaderboard  - read-only views
  Store                              - browse + buy account keys with coins
  Coinflip / Dice                    - casino-style gambling (34% win, 2x)
  Pay                                - send coins to another member
"""
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from payments import TopUpView, WalletAdminView, fmt_usd, wallet_admin_embed

log = logging.getLogger("reseller-bot.coins")

DEFAULTS = {
    "required_status": config.DEFAULT_REQUIRED_STATUS,
    "reward_seconds": config.DEFAULT_REWARD_HOURS * 3600.0,
    "coins_per_reward": config.DEFAULT_COINS_PER_REWARD,
    "log_channel_id": config.DEFAULT_LOG_CHANNEL_ID,
    "eligible_statuses": config.DEFAULT_ELIGIBLE_STATUSES,
}

_STATUS_MAP = {
    "online": discord.Status.online,
    "dnd": discord.Status.dnd,
    "idle": discord.Status.idle,
    "offline": discord.Status.offline,
}

GAMBLE_COOLDOWN_SECONDS = 15


# --------------------------- helpers ---------------------------
def parse_statuses(raw):
    out = set()
    for part in (raw or "").split(","):
        s = _STATUS_MAP.get(part.strip().lower())
        if s:
            out.add(s)
    return out or {discord.Status.online, discord.Status.dnd}


def get_custom_status_text(member):
    for activity in getattr(member, "activities", ()) or ():
        if isinstance(activity, discord.CustomActivity):
            return activity.name or ""
    return ""


def human_duration(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def fmt_coins(value):
    """Format a (possibly fractional) coin amount, up to 3 decimals."""
    s = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return s or "0"


def extract_stock(stock, account_type):
    """Pull a numeric stock count for an account type from the /stock payload."""
    value = stock.get(account_type) if isinstance(stock, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("stock", "count", "available", "amount", "qty"):
            v = value.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
    return None


# --------------------------- cog: earning engine ---------------------------
class CoinsCog(commands.Cog):
    """Presence tracking + reward accrual (ported from status-coin-bot)."""

    def __init__(self, bot):
        self.bot = bot
        # (guild_id, user_id) -> unix timestamp when they became eligible.
        self.eligible_since = {}
        self._settings_cache = {}
        # Per-user in-flight purchase / gamble guards.
        self._buying = set()
        self._last_gamble = {}
        self.reward_loop.start()

    def cog_unload(self):
        self.reward_loop.cancel()

    async def get_guild_settings(self, guild_id):
        cached = self._settings_cache.get(guild_id)
        if cached:
            return cached
        s = await self.bot.db.get_coin_settings(guild_id, DEFAULTS)
        self._settings_cache[guild_id] = s
        return s

    def invalidate_settings(self, guild_id):
        self._settings_cache.pop(guild_id, None)

    # ----- eligibility + time accounting -----
    def is_eligible(self, member, settings):
        if member is None or member.bot:
            return False
        if member.status not in parse_statuses(settings["eligible_statuses"]):
            return False
        text = get_custom_status_text(member)
        if not text:
            return False
        return settings["required_status"].strip().lower() in text.lower()

    def mark_eligible(self, guild_id, user_id, now=None):
        key = (guild_id, user_id)
        if key not in self.eligible_since:
            self.eligible_since[key] = now or time.time()

    async def flush_user(self, guild_id, user_id, now=None):
        """Move accrued eligible time into the DB; keep the timer running."""
        now = now or time.time()
        key = (guild_id, user_id)
        since = self.eligible_since.get(key)
        if since is None:
            return
        elapsed = now - since
        self.eligible_since[key] = now
        if elapsed <= 0:
            return
        u = await self.bot.db.get_user(guild_id, user_id)
        await self.bot.db.upsert_user(
            guild_id,
            user_id,
            total_eligible_seconds=u["total_eligible_seconds"] + elapsed,
            updated_at=now,
        )

    async def mark_ineligible(self, guild_id, user_id, now=None):
        key = (guild_id, user_id)
        if key in self.eligible_since:
            await self.flush_user(guild_id, user_id, now)
            self.eligible_since.pop(key, None)

    async def process_rewards(self, guild_id, user_id, settings):
        """Credit coins continuously for newly-accrued eligible time.

        Coins accrue fractionally (e.g. 0.001 at a time) so balances tick up
        toward coins_per_reward every reward interval. Returns the number of
        whole coins completed since the last call (used for announcements).
        """
        reward_seconds = settings["reward_seconds"]
        if reward_seconds <= 0:
            return 0
        u = await self.bot.db.get_user(guild_id, user_id)
        total = u["total_eligible_seconds"]
        credited = u.get("credited_seconds") or 0.0
        if credited <= 0 and u["rewards_count"] > 0:
            # Pre-fractional rows were only paid for whole intervals.
            credited = u["rewards_count"] * reward_seconds
        delta = total - credited
        if delta <= 0:
            return 0
        per = float(settings["coins_per_reward"])
        gained = delta / reward_seconds * per
        target = int(total // reward_seconds)
        whole = max(0, target - u["rewards_count"]) * int(settings["coins_per_reward"])
        await self.bot.db.upsert_user(
            guild_id,
            user_id,
            coins=u["coins"] + gained,
            credited_seconds=total,
            rewards_count=max(target, u["rewards_count"]),
        )
        return whole

    async def announce_reward(self, guild, user_id, gained, settings):
        u = await self.bot.db.get_user(guild.id, user_id)
        log.info("Rewarded %s coin(s) to %s in guild %s", gained, user_id, guild.id)
        chan_id = settings.get("log_channel_id")
        if not chan_id:
            return
        channel = guild.get_channel(int(chan_id))
        if channel is None:
            return
        member = guild.get_member(user_id)
        who = member.mention if member else f"<@{user_id}>"
        embed = discord.Embed(
            title=f"{config.COIN_EMOJI} Reward Earned!",
            description=(
                f"{who} earned **{gained} {config.COIN_NAME}(s)** for staying "
                f"online with the required status.\n"
                f"New balance: **{fmt_coins(u['coins'])} {config.COIN_NAME}(s)**"
            ),
            color=config.EMBED_COLOR,
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def live_total(self, guild_id, user_id, settings):
        """DB total plus any not-yet-flushed live time."""
        u = await self.bot.db.get_user(guild_id, user_id)
        total = u["total_eligible_seconds"]
        since = self.eligible_since.get((guild_id, user_id))
        if since:
            total += time.time() - since
        reward_seconds = settings["reward_seconds"]
        into = total % reward_seconds if reward_seconds else 0
        remaining = (reward_seconds - into) if reward_seconds else 0
        pct = (into / reward_seconds * 100) if reward_seconds else 0
        return u, total, remaining, pct

    # ----- background loop -----
    @tasks.loop(seconds=config.TICK_SECONDS)
    async def reward_loop(self):
        try:
            now = time.time()
            for guild in self.bot.guilds:
                settings = await self.get_guild_settings(guild.id)
                keys = [k for k in list(self.eligible_since.keys()) if k[0] == guild.id]
                for (gid, uid) in keys:
                    await self.flush_user(gid, uid, now)
                    gained = await self.process_rewards(gid, uid, settings)
                    if gained > 0:
                        await self.announce_reward(guild, uid, gained, settings)
        except Exception:  # noqa: BLE001 - the loop must never die
            log.exception("reward_loop tick failed; will retry next tick")

    @reward_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    # ----- events -----
    @commands.Cog.listener()
    async def on_ready(self):
        now = time.time()
        for guild in self.bot.guilds:
            settings = await self.get_guild_settings(guild.id)
            for member in guild.members:
                if member.bot:
                    continue
                if self.is_eligible(member, settings):
                    self.mark_eligible(guild.id, member.id, now)
                else:
                    await self.mark_ineligible(guild.id, member.id, now)
        log.info("Initialised coin tracking for %d eligible member(s)", len(self.eligible_since))

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        member = after
        if member.bot or member.guild is None:
            return
        guild_id = member.guild.id
        settings = await self.get_guild_settings(guild_id)
        now = time.time()
        eligible = self.is_eligible(member, settings)
        key = (guild_id, member.id)
        was = key in self.eligible_since

        if eligible and not was:
            self.mark_eligible(guild_id, member.id, now)
        elif not eligible and was:
            await self.mark_ineligible(guild_id, member.id, now)
        elif eligible and was:
            await self.flush_user(guild_id, member.id, now)
            gained = await self.process_rewards(guild_id, member.id, settings)
            if gained > 0:
                await self.announce_reward(member.guild, member.id, gained, settings)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        self.eligible_since.pop((member.guild.id, member.id), None)


# --------------------------- embed builders ---------------------------
async def balance_embed(cog, guild_id, user):
    settings = await cog.get_guild_settings(guild_id)
    u, total, remaining, pct = await cog.live_total(guild_id, user.id, settings)
    embed = discord.Embed(
        title=f"{config.COIN_EMOJI} {user.display_name}'s Balance",
        color=config.EMBED_COLOR,
    )
    embed.add_field(name="Coins", value=f"**{fmt_coins(u['coins'])}** {config.COIN_NAME}(s)", inline=True)
    embed.add_field(name="Rewards earned", value=str(u["rewards_count"]), inline=True)
    embed.add_field(name="Total online time", value=human_duration(total), inline=False)
    embed.add_field(
        name="Next coin in",
        value=f"{human_duration(remaining)}  ({pct:.1f}% there)",
        inline=False,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


async def eligibility_embed(cog, interaction):
    member = interaction.guild.get_member(interaction.user.id) or interaction.user
    settings = await cog.get_guild_settings(interaction.guild_id)
    statuses = parse_statuses(settings["eligible_statuses"])
    status_ok = getattr(member, "status", None) in statuses
    text = get_custom_status_text(member)
    text_ok = settings["required_status"].strip().lower() in (text or "").lower()
    eligible = status_ok and text_ok
    embed = discord.Embed(
        title=f"Eligibility Check — {member.display_name}",
        color=discord.Color.green() if eligible else discord.Color.red(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="Online status",
        value=f"{'✅' if status_ok else '❌'} `{getattr(member, 'status', 'unknown')}` "
        f"(need one of: {settings['eligible_statuses']})",
        inline=False,
    )
    embed.add_field(
        name="Required status text",
        value=f"{'✅' if text_ok else '❌'} must contain: `{settings['required_status']}`\n"
        f"status shown: `{text or '(none set)'}`",
        inline=False,
    )
    embed.add_field(
        name="Currently earning?",
        value="✅ **Yes** — keep it up!" if eligible else "❌ **No** — fix the above",
        inline=False,
    )
    return embed


async def leaderboard_embed(bot, guild, limit=10):
    rows = await bot.db.leaderboard(guild.id, limit)
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = []
    for i, row in enumerate(rows):
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        prefix = medals[i] if i < 3 else f"`#{i + 1}`"
        lines.append(f"{prefix} **{name}** \u2014 {fmt_coins(row['coins'])} {config.COIN_NAME}(s)")
    return discord.Embed(
        title=f"{config.COIN_EMOJI} Leaderboard",
        description="\n".join(lines) if lines else "No one has earned coins yet.",
        color=config.EMBED_COLOR,
    )


async def howto_embed(cog, guild_id):
    s = await cog.get_guild_settings(guild_id)
    hours = s["reward_seconds"] / 3600
    return discord.Embed(
        title="How to Earn Coins",
        color=config.EMBED_COLOR,
        description=(
            f"**1.** Set your Discord **custom status** to include:\n"
            f"> `{s['required_status']}`\n\n"
            f"**2.** Stay **{s['eligible_statuses']}** (i.e. actually online).\n\n"
            f"**3.** For every **{hours:g} hours** of online time *with the status*, "
            f"you earn **{s['coins_per_reward']} {config.COIN_NAME}(s)**.\n\n"
            f"Only time while you're online **and** showing the status counts. "
            f"Use the **Check** button to confirm you're set up, and **Balance** "
            f"to track progress."
        ),
    )


async def store_embed(bot):
    stock = {}
    if config.NFA_API_KEY:
        try:
            stock = await bot.fetch_stock()
        except Exception as exc:  # noqa: BLE001
            log.warning("Store stock fetch failed: %s", exc)
    lines = []
    for tier in config.STORE_TIERS:
        at = tier.get("account_type", "")
        label = tier.get("label", at)
        cost = tier.get("cost", 0)
        count = extract_stock(stock, at)
        if count is None:
            dot, stock_str = "\u26AA", "stock: n/a"
        elif count > 0:
            dot, stock_str = "\U0001F7E2", "in stock"
        else:
            dot, stock_str = "\U0001F534", "out of stock"
        lines.append(
            f"{dot} **{label}** \u2014 **{cost}** {config.COIN_EMOJI} \u00b7 {stock_str}"
        )
    embed = discord.Embed(
        title="\U0001F6D2 Account Store",
        description="\n".join(lines) if lines else "No products configured.",
        color=config.EMBED_COLOR,
    )
    embed.set_footer(text="Pick a product below to buy \u00b7 stock is live")
    return embed


# --------------------------- purchase flow ---------------------------
async def do_purchase(cog, interaction, account_type):
    bot = cog.bot
    tier = next((t for t in config.STORE_TIERS if t["account_type"] == account_type), None)
    if tier is None:
        await interaction.response.send_message("Unknown product.", ephemeral=True)
        return
    if not config.NFA_API_KEY:
        await interaction.response.send_message(
            "The store isn't configured yet (an admin must set `NFA_API_KEY`).",
            ephemeral=True,
        )
        return

    cost = int(tier.get("cost", 0))
    guild_id, uid = interaction.guild_id, interaction.user.id
    key = (guild_id, uid)
    if key in cog._buying:
        await interaction.response.send_message(
            "You already have a purchase in progress \u2014 please wait.", ephemeral=True
        )
        return

    u = await bot.db.get_user(guild_id, uid)
    if u["coins"] < cost:
        await interaction.response.send_message(
            f"You need **{cost}** {config.COIN_NAME}(s) but only have **{fmt_coins(u['coins'])}**.",
            ephemeral=True,
        )
        return

    cog._buying.add(key)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        # Stock pre-check (no charge if out of stock).
        try:
            stock = await bot.fetch_stock()
            count = extract_stock(stock, account_type)
            if count is not None and count <= 0:
                await interaction.followup.send(
                    "That account is **out of stock** right now. You were not charged.",
                    ephemeral=True,
                )
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("stock precheck failed: %s", exc)

        # Reserve coins; refund on any failure below.
        await bot.db.add_coins(guild_id, uid, -cost)
        try:
            _, data = await bot._nfa(
                "POST",
                "/api/v1/create_keys",
                json={"account_type": account_type, "amount": 1},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            await bot.db.add_coins(guild_id, uid, cost)
            log.warning("create_keys error: %s", exc)
            await interaction.followup.send(
                "The store errored while generating your key. "
                "You were **refunded** \u2014 please try again shortly.",
                ephemeral=True,
            )
            return

        keys = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("status") != "success" or not keys:
            await bot.db.add_coins(guild_id, uid, cost)
            msg = data.get("message") if isinstance(data, dict) else "Unknown error"
            await interaction.followup.send(
                f"Purchase failed: {msg}\nYou were **refunded**.", ephemeral=True
            )
            return

        await deliver_key(interaction, tier, keys[0])
    finally:
        cog._buying.discard(key)


async def deliver_key(interaction, tier, key):
    """DM the buyer their key and post a public purchase announcement."""
    label = tier.get("label", tier["account_type"])
    content = (
        f"\U0001F389 **{label}** \u2014 here's your key!\n\n"
        f"**Your key:** ||`{key}`||\n\n"
        f"**How to use it:**\n"
        f"1. Go to {config.ACTIVATION_URL}\n"
        f"2. Enter your key there to activate and download your account.\n\n"
        f"Keep this key private \u2014 treat it like cash."
    )

    dm_ok = False
    try:
        dm = await interaction.user.create_dm()
        await dm.send(content=content)
        dm_ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("DM delivery failed: %s", exc)

    if interaction.channel is not None:
        announce = discord.Embed(
            title=f"{config.COIN_EMOJI} New purchase!",
            description=f"{interaction.user.mention} just bought **{label}** "
            f"for **{tier.get('cost', 0)}** {config.COIN_NAME}(s)!",
            color=config.EMBED_COLOR,
        )
        try:
            await interaction.channel.send(embed=announce)
        except discord.HTTPException as exc:
            log.warning("public announce failed: %s", exc)

    try:
        if dm_ok:
            await interaction.followup.send(
                "\u2705 Purchase complete \u2014 check your **DMs** for your key!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "\u2705 Purchase complete! I couldn't DM you (are your DMs open?), "
                "so here's your key privately:\n\n" + content,
                ephemeral=True,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("followup failed: %s", exc)


# --------------------------- gambling ---------------------------
async def do_gamble(cog, interaction, game, bet):
    """Casino-style round: 34% win chance, 2x payout, max bet 5 (config)."""
    guild_id, uid = interaction.guild_id, interaction.user.id
    now = time.time()
    last = cog._last_gamble.get((guild_id, uid), 0)
    wait = GAMBLE_COOLDOWN_SECONDS - (now - last)
    if wait > 0:
        await interaction.response.send_message(
            f"\u23F3 Slow down \u2014 you can play again in **{wait:.0f}s**.", ephemeral=True
        )
        return
    if bet < 1 or bet > config.GAMBLE_MAX_BET:
        await interaction.response.send_message(
            f"Bets must be between 1 and {config.GAMBLE_MAX_BET} {config.COIN_NAME}(s).",
            ephemeral=True,
        )
        return

    ok, _ = await cog.bot.db.try_adjust_coins(guild_id, uid, -bet)
    if not ok:
        await interaction.response.send_message(
            f"You don't have **{bet}** {config.COIN_NAME}(s) to stake.", ephemeral=True
        )
        return

    cog._last_gamble[(guild_id, uid)] = now
    won = random.random() < config.GAMBLE_WIN_CHANCE
    payout = int(bet * config.GAMBLE_MULTIPLIER) if won else 0
    if payout:
        await cog.bot.db.add_coins(guild_id, uid, payout)
    u = await cog.bot.db.get_user(guild_id, uid)

    if game == "coinflip":
        flavor = "\U0001FA99 The coin lands on **your side**!" if won else "\U0001FA99 The coin lands **against you**."
    else:
        roll = random.randint(5, 6) if won else random.randint(1, 4)
        flavor = f"\U0001F3B2 You rolled a **{roll}** \u2014 {'winner!' if won else 'no luck.'}"

    if won:
        result = f"{flavor}\nYou won **{payout}** {config.COIN_NAME}(s)! \U0001F389"
        color = discord.Color.green()
    else:
        result = f"{flavor}\nYou lost your **{bet}** {config.COIN_NAME}(s) stake."
        color = discord.Color.red()

    embed = discord.Embed(
        title=f"{'Coin Flip' if game == 'coinflip' else 'Dice'} \u2014 bet {bet}",
        description=f"{result}\n\nBalance: **{fmt_coins(u['coins'])}** {config.COIN_NAME}(s)",
        color=color,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


class BetView(discord.ui.View):
    """Pick a stake for a gambling game."""

    def __init__(self, cog, owner_id, game):
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.game = game
        self.bet_select = discord.ui.Select(
            placeholder=f"Stake (1-{config.GAMBLE_MAX_BET} {config.COIN_NAME}s)",
            options=[
                discord.SelectOption(label=f"{n} {config.COIN_NAME}(s)", value=str(n))
                for n in range(1, config.GAMBLE_MAX_BET + 1)
            ],
        )
        self.bet_select.callback = self._on_bet
        self.add_item(self.bet_select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("These controls aren't yours.", ephemeral=True)
            return False
        return True

    async def _on_bet(self, interaction):
        await do_gamble(self.cog, interaction, self.game, int(self.bet_select.values[0]))


# --------------------------- pay flow ---------------------------
class PayView(discord.ui.View):
    """Pick a member and an amount, then send coins."""

    def __init__(self, cog, owner_id):
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.recipient = None
        self.amount = None

        self.user_select = discord.ui.UserSelect(placeholder="Who gets the coins?")
        self.user_select.callback = self._on_user
        self.add_item(self.user_select)

        self.amount_select = discord.ui.Select(
            placeholder="How many coins?",
            options=[
                discord.SelectOption(label=f"{n} {config.COIN_NAME}(s)", value=str(n))
                for n in (1, 2, 3, 4, 5, 10, 25)
            ],
        )
        self.amount_select.callback = self._on_amount
        self.add_item(self.amount_select)

        self.send_button = discord.ui.Button(label="Send", style=discord.ButtonStyle.success)
        self.send_button.callback = self._on_send
        self.add_item(self.send_button)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("These controls aren't yours.", ephemeral=True)
            return False
        return True

    async def _on_user(self, interaction):
        self.recipient = self.user_select.values[0]
        await interaction.response.defer()

    async def _on_amount(self, interaction):
        self.amount = int(self.amount_select.values[0])
        await interaction.response.defer()

    async def _on_send(self, interaction):
        if self.recipient is None or self.amount is None:
            await interaction.response.send_message(
                "Pick a recipient and an amount first.", ephemeral=True
            )
            return
        if self.recipient.id == interaction.user.id or self.recipient.bot:
            await interaction.response.send_message("Invalid recipient.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        ok, _ = await self.cog.bot.db.try_adjust_coins(guild_id, interaction.user.id, -self.amount)
        if not ok:
            await interaction.response.send_message("You don't have enough coins.", ephemeral=True)
            return
        await self.cog.bot.db.add_coins(guild_id, self.recipient.id, self.amount)
        await interaction.response.send_message(
            f"{config.COIN_EMOJI} {interaction.user.mention} sent **{self.amount}** "
            f"{config.COIN_NAME}(s) to {self.recipient.mention}!"
        )
        self.stop()


# --------------------------- store view ---------------------------
class StoreView(discord.ui.View):
    def __init__(self, cog, owner_id):
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = owner_id
        self.product_select = discord.ui.Select(
            placeholder="Buy a product with your coins",
            options=[
                discord.SelectOption(
                    label=f"{t.get('label', t['account_type'])} ({t.get('cost', 0)} {config.COIN_NAME})"[:100],
                    value=t["account_type"],
                )
                for t in config.STORE_TIERS
            ][:25],
        )
        self.product_select.callback = self._on_product
        self.add_item(self.product_select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("These controls aren't yours.", ephemeral=True)
            return False
        return True

    async def _on_product(self, interaction):
        await do_purchase(self.cog, interaction, self.product_select.values[0])


# --------------------------- /coin hub ---------------------------
class CoinMenuView(discord.ui.View):
    def __init__(self, cog, owner_id):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Open your own menu with `/coin`.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Balance", emoji="\U0001F4B0", style=discord.ButtonStyle.primary, row=0)
    async def balance_btn(self, interaction, button):
        embed = await balance_embed(self.cog, interaction.guild_id, interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Check", emoji="\u2705", style=discord.ButtonStyle.primary, row=0)
    async def check_btn(self, interaction, button):
        embed = await eligibility_embed(self.cog, interaction)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Leaderboard", emoji="\U0001F3C6", style=discord.ButtonStyle.primary, row=0)
    async def leaderboard_btn(self, interaction, button):
        embed = await leaderboard_embed(self.cog.bot, interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="How to earn", emoji="\u2754", style=discord.ButtonStyle.secondary, row=0)
    async def howto_btn(self, interaction, button):
        embed = await howto_embed(self.cog, interaction.guild_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Store", emoji="\U0001F6D2", style=discord.ButtonStyle.success, row=1)
    async def store_btn(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await store_embed(self.cog.bot)
        await interaction.followup.send(
            embed=embed, view=StoreView(self.cog, interaction.user.id), ephemeral=True
        )

    @discord.ui.button(label="Coinflip", emoji="\U0001FA99", style=discord.ButtonStyle.danger, row=1)
    async def coinflip_btn(self, interaction, button):
        await interaction.response.send_message(
            f"\U0001FA99 **Coin Flip** \u2014 {config.GAMBLE_WIN_CHANCE:.0%} win chance, "
            f"**{config.GAMBLE_MULTIPLIER:g}x** payout. Pick your stake:",
            view=BetView(self.cog, interaction.user.id, "coinflip"),
            ephemeral=True,
        )

    @discord.ui.button(label="Dice", emoji="\U0001F3B2", style=discord.ButtonStyle.danger, row=1)
    async def dice_btn(self, interaction, button):
        await interaction.response.send_message(
            f"\U0001F3B2 **Dice** \u2014 {config.GAMBLE_WIN_CHANCE:.0%} win chance, "
            f"**{config.GAMBLE_MULTIPLIER:g}x** payout. Pick your stake:",
            view=BetView(self.cog, interaction.user.id, "dice"),
            ephemeral=True,
        )

    @discord.ui.button(label="Top Up", emoji="\U0001F4B3", style=discord.ButtonStyle.success, row=2)
    async def topup_btn(self, interaction, button):
        payments = self.cog.bot.get_cog("PaymentsCog")
        if payments is None or not payments.enabled:
            await interaction.response.send_message(
                "Top-ups aren't configured yet (an admin must set `HOT_WALLET_MNEMONIC`).",
                ephemeral=True,
            )
            return
        packages = " \u00b7 ".join(
            f"**{p['coins']}** for {fmt_usd(p['usd'])}" for p in config.TOPUP_PACKAGES
        )
        await interaction.response.send_message(
            f"\U0001F4B3 **Buy {config.COIN_NAME}s with crypto** (BTC / ETH / SOL / LTC)\n"
            f"{packages}\n\nPick a package, then the coin you want to pay with:",
            view=TopUpView(payments, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Pay", emoji="\U0001F381", style=discord.ButtonStyle.secondary, row=1)
    async def pay_btn(self, interaction, button):
        await interaction.response.send_message(
            "Send coins to another member:",
            view=PayView(self.cog, interaction.user.id),
            ephemeral=True,
        )


async def hub_embed(cog, interaction):
    settings = await cog.get_guild_settings(interaction.guild_id)
    u = await cog.bot.db.get_user(interaction.guild_id, interaction.user.id)
    hours = settings["reward_seconds"] / 3600
    return discord.Embed(
        title=f"{config.COIN_EMOJI} Coins",
        color=config.EMBED_COLOR,
        description=(
            f"Your balance: **{fmt_coins(u['coins'])}** {config.COIN_NAME}(s)\n\n"
            f"Earn **{settings['coins_per_reward']} {config.COIN_NAME}(s)** per "
            f"**{hours:g}h** online with `{settings['required_status']}` in your status.\n"
            f"Spend them in the **Store**, try your luck with **Coinflip** / **Dice** "
            f"({config.GAMBLE_WIN_CHANCE:.0%} win, {config.GAMBLE_MULTIPLIER:g}x, "
            f"max bet {config.GAMBLE_MAX_BET}), **Pay** a friend, or **Top Up** "
            f"with crypto (BTC / ETH / SOL / LTC)."
        ),
    )


# --------------------------- admin group ---------------------------
@app_commands.guild_only()
class CoinAdminGroup(app_commands.Group):
    def __init__(self, cog):
        super().__init__(name="coin-admin", description="Coin system administration")
        self.cog = cog

    async def interaction_check(self, interaction):
        if config.ADMIN_USER_IDS and interaction.user.id in config.ADMIN_USER_IDS:
            return True
        if interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "\u26D4 You're not authorised to use the coin admin commands.", ephemeral=True
        )
        return False

    @app_commands.command(name="addcoins", description="Add (or subtract) a user's coins")
    @app_commands.describe(user="Member", amount="Amount (use a negative number to remove)")
    async def addcoins(self, interaction, user: discord.Member, amount: int):
        new = await self.cog.bot.db.add_coins(interaction.guild_id, user.id, amount)
        await interaction.response.send_message(
            f"{user.display_name} now has **{fmt_coins(new)}** {config.COIN_NAME}(s).", ephemeral=True
        )

    @app_commands.command(name="setcoins", description="Set a user's coin balance")
    @app_commands.describe(user="Member", amount="New balance")
    async def setcoins(self, interaction, user: discord.Member, amount: int):
        new = await self.cog.bot.db.set_coins(interaction.guild_id, user.id, amount)
        await interaction.response.send_message(
            f"{user.display_name} now has **{fmt_coins(new)}** {config.COIN_NAME}(s).", ephemeral=True
        )

    @app_commands.command(name="setrequiredstatus", description="Set the required custom-status text")
    @app_commands.describe(text="Text members must show in their custom status")
    async def setrequiredstatus(self, interaction, text: str):
        await self.cog.bot.db.update_coin_setting(interaction.guild_id, "required_status", text)
        self.cog.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Required status text set to `{text}`.", ephemeral=True
        )

    @app_commands.command(name="setrewardhours", description="Hours of online time per coin reward")
    @app_commands.describe(hours="Hours per reward")
    async def setrewardhours(self, interaction, hours: float):
        if hours <= 0:
            await interaction.response.send_message("Hours must be positive.", ephemeral=True)
            return
        await self.cog.bot.db.update_coin_setting(
            interaction.guild_id, "reward_seconds", hours * 3600.0
        )
        self.cog.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Members now earn a reward every **{hours:g}h**.", ephemeral=True
        )

    @app_commands.command(name="setlogchannel", description="Channel for reward announcements")
    @app_commands.describe(channel="Channel (leave empty to disable)")
    async def setlogchannel(self, interaction, channel: discord.TextChannel = None):
        await self.cog.bot.db.update_coin_setting(
            interaction.guild_id, "log_channel_id", channel.id if channel else None
        )
        self.cog.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Reward announcements {'go to ' + channel.mention if channel else 'are disabled'}.",
            ephemeral=True,
        )

    @app_commands.command(name="wallet", description="Hot wallet: balances, payout addresses, withdrawals")
    async def wallet(self, interaction):
        payments = self.cog.bot.get_cog("PaymentsCog")
        if payments is None or not payments.enabled:
            await interaction.response.send_message(
                "Top-ups aren't configured: set the `HOT_WALLET_MNEMONIC` env var "
                "(the bot logs a freshly generated seed on boot).",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await wallet_admin_embed(payments)
        await interaction.followup.send(
            embed=embed, view=WalletAdminView(payments, interaction.user.id), ephemeral=True
        )

    @app_commands.command(name="settings", description="View current coin settings")
    async def settings(self, interaction):
        s = await self.cog.get_guild_settings(interaction.guild_id)
        embed = discord.Embed(title="Coin settings", color=config.EMBED_COLOR)
        embed.add_field(name="Required status", value=f"`{s['required_status']}`", inline=False)
        embed.add_field(name="Hours per reward", value=f"{s['reward_seconds'] / 3600:g}", inline=True)
        embed.add_field(name="Coins per reward", value=str(s["coins_per_reward"]), inline=True)
        embed.add_field(name="Eligible statuses", value=s["eligible_statuses"], inline=True)
        embed.add_field(
            name="Log channel",
            value=f"<#{s['log_channel_id']}>" if s["log_channel_id"] else "(off)",
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------- registration ---------------------------
async def setup(bot):
    cog = CoinsCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(CoinAdminGroup(cog))

    @bot.tree.command(name="coin", description="Coins hub: balance, store, gambling, pay \u2014 all in one place")
    @app_commands.guild_only()
    async def coin(interaction: discord.Interaction):
        embed = await hub_embed(cog, interaction)
        await interaction.response.send_message(
            embed=embed, view=CoinMenuView(cog, interaction.user.id), ephemeral=True
        )
