"""Crypto top-ups + hot-wallet admin panel.

Members buy coin packages with BTC / ETH / SOL / LTC. Each invoice gets a
unique amount (base + random dust) sent to the bot's hot-wallet address for
that chain; a watcher loop matches confirmed on-chain payments to invoices
and credits coins automatically.

Admins get a wallet panel (via `/coin-admin wallet`) showing live balances,
with per-coin payout addresses, a manual Withdraw (sweep) button, and an
auto-withdraw USD threshold.
"""
import logging
import random
import time

import discord
from discord.ext import commands, tasks

import config
import wallets
from wallets import ChainError, WalletManager, generate_mnemonic

log = logging.getLogger("reseller-bot.payments")

CURRENCIES = [cls.code for cls in wallets.CHAIN_CLASSES]
CURRENCY_EMOJI = {"BTC": "\u20BF", "ETH": "\u27E0", "SOL": "\u25CE", "LTC": "\u0141"}


def fmt_usd(v):
    return f"${v:,.2f}"


class PaymentsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.wallets = None

    async def cog_load(self):
        if not config.HOT_WALLET_MNEMONIC:
            log.warning(
                "HOT_WALLET_MNEMONIC not set - crypto top-ups disabled. "
                "Here is a freshly generated seed you can use (set it as the "
                "HOT_WALLET_MNEMONIC env var and KEEP IT SECRET):\n\n    %s\n",
                generate_mnemonic(),
            )
            return
        try:
            self.wallets = WalletManager(self.bot.session, config.HOT_WALLET_MNEMONIC)
        except Exception:  # noqa: BLE001
            log.exception("Invalid HOT_WALLET_MNEMONIC - crypto top-ups disabled.")
            return
        for code, chain in self.wallets.chains.items():
            log.info("Hot wallet %s address: %s", code, chain.address)
        self.watch_loop.start()
        self.auto_withdraw_loop.start()

    def cog_unload(self):
        self.watch_loop.cancel()
        self.auto_withdraw_loop.cancel()

    @property
    def enabled(self):
        return self.wallets is not None

    # ------------------- invoice creation -------------------
    async def create_invoice(self, guild_id, user_id, package, currency):
        chain = self.wallets.chain(currency)
        prices = await self.wallets.prices()
        price = prices.get(currency)
        if not price:
            raise ChainError(f"no live price for {currency}")
        base_units = chain.to_units(package["usd"] / price)
        for _ in range(50):
            dust = random.randint(*chain.dust_range) * chain.dust_unit
            amount_units = base_units + dust
            if not await self.bot.db.pending_amount_exists(currency, amount_units):
                break
        else:
            raise ChainError("could not allocate a unique amount, try again")
        topup_id = await self.bot.db.create_topup(
            guild_id, user_id, int(package["coins"]), float(package["usd"]),
            currency, amount_units,
        )
        return topup_id, chain, amount_units

    # ------------------- payment watching -------------------
    @tasks.loop(seconds=config.TOPUP_POLL_SECONDS)
    async def watch_loop(self):
        try:
            pending = await self.bot.db.pending_topups()
            if not pending:
                return
            now = time.time()
            expire_s = config.TOPUP_EXPIRE_MINUTES * 60
            by_currency = {}
            for row in pending:
                if now - row["created_at"] > expire_s:
                    await self.bot.db.mark_topup(row["id"], "expired")
                    continue
                by_currency.setdefault(row["currency"], []).append(row)
            for currency, rows in by_currency.items():
                chain = self.wallets.chain(currency)
                oldest = min(r["created_at"] for r in rows)
                try:
                    payments = await chain.incoming(oldest - 600)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s incoming check failed: %s", currency, exc)
                    continue
                for txid, amount in payments:
                    row = next((r for r in rows if r["amount_units"] == amount), None)
                    if row is None:
                        continue
                    if await self.bot.db.txid_already_used(currency, txid):
                        continue
                    if await self.bot.db.mark_topup(row["id"], "paid", txid):
                        await self._credit(row, chain, txid)
        except Exception:  # noqa: BLE001
            log.exception("watch_loop tick failed; will retry next tick")

    async def _credit(self, row, chain, txid):
        new = await self.bot.db.add_coins(row["guild_id"], row["user_id"], row["coins"])
        log.info(
            "Top-up #%s paid: %s %s -> %s coins for user %s (tx %s)",
            row["id"], chain.format_amount(row["amount_units"]), chain.code,
            row["coins"], row["user_id"], txid,
        )
        user = self.bot.get_user(row["user_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(row["user_id"])
            except discord.HTTPException:
                return
        try:
            await user.send(
                f"\u2705 **Payment received!** Your top-up of "
                f"**{chain.format_amount(row['amount_units'])} {chain.code}** "
                f"({fmt_usd(row['usd'])}) confirmed.\n"
                f"**+{row['coins']} {config.COIN_NAME}(s)** added — new balance: "
                f"**{round(new, 3)}**."
            )
        except discord.HTTPException:
            pass

    # ------------------- auto withdraw -------------------
    @tasks.loop(seconds=config.AUTO_WITHDRAW_POLL_SECONDS)
    async def auto_withdraw_loop(self):
        try:
            settings = await self.bot.db.get_wallet_settings()
            prices = await self.wallets.prices()
            for currency, s in settings.items():
                threshold = s.get("auto_threshold_usd") or 0
                address = s.get("payout_address")
                if threshold <= 0 or not address or currency not in self.wallets.chains:
                    continue
                chain = self.wallets.chain(currency)
                price = prices.get(currency)
                if not price:
                    continue
                bal = await chain.balance()
                usd = bal / 10 ** chain.decimals * price
                if usd < threshold:
                    continue
                try:
                    txid = await chain.sweep(address)
                    log.info("Auto-withdrew %s %s (%s) tx %s",
                             chain.format_amount(bal), currency, fmt_usd(usd), txid)
                except ChainError as exc:
                    log.warning("Auto-withdraw %s skipped: %s", currency, exc)
        except Exception:  # noqa: BLE001
            log.exception("auto_withdraw tick failed; will retry next tick")

    @watch_loop.before_loop
    @auto_withdraw_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


# ------------------- member top-up UI -------------------
class TopUpView(discord.ui.View):
    """Pick a package + coin, then get an invoice with address & amount."""

    def __init__(self, cog, owner_id):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.package = None

        self.package_select = discord.ui.Select(
            placeholder="How many coins?",
            options=[
                discord.SelectOption(
                    label=f"{p['coins']} {config.COIN_NAME}(s) — {fmt_usd(p['usd'])}",
                    value=str(i),
                )
                for i, p in enumerate(config.TOPUP_PACKAGES)
            ][:25],
        )
        self.package_select.callback = self._on_package
        self.add_item(self.package_select)

        self.coin_select = discord.ui.Select(
            placeholder="Pay with which crypto?",
            options=[
                discord.SelectOption(
                    label=f"{wallets_cls.name} ({wallets_cls.code})",
                    value=wallets_cls.code,
                    emoji=CURRENCY_EMOJI.get(wallets_cls.code),
                )
                for wallets_cls in wallets.CHAIN_CLASSES
            ],
        )
        self.coin_select.callback = self._on_coin
        self.add_item(self.coin_select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("These controls aren't yours.", ephemeral=True)
            return False
        return True

    async def _on_package(self, interaction):
        self.package = config.TOPUP_PACKAGES[int(self.package_select.values[0])]
        await interaction.response.defer()

    async def _on_coin(self, interaction):
        if self.package is None:
            await interaction.response.send_message(
                "Pick a coin package first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        currency = self.coin_select.values[0]
        try:
            topup_id, chain, amount_units = await self.cog.create_invoice(
                interaction.guild_id, interaction.user.id, self.package, currency
            )
        except ChainError as exc:
            await interaction.followup.send(f"Couldn't create invoice: {exc}", ephemeral=True)
            return
        amount = chain.format_amount(amount_units)
        embed = discord.Embed(
            title=f"\U0001F9FE Top-up invoice #{topup_id}",
            color=config.EMBED_COLOR,
            description=(
                f"Send **exactly** this amount — it's how your payment is matched:\n\n"
                f"**Amount:** `{amount}` {chain.code}\n"
                f"**Address:** `{chain.address}`\n\n"
                f"You'll get **{self.package['coins']} {config.COIN_NAME}(s)** "
                f"({fmt_usd(self.package['usd'])}) credited automatically after "
                f"1 network confirmation. I'll DM you when it lands.\n\n"
                f"\u23F3 Invoice expires in **{config.TOPUP_EXPIRE_MINUTES} minutes**. "
                f"Send one single payment of the exact amount."
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------- admin wallet panel -------------------
class PayoutAddressModal(discord.ui.Modal):
    def __init__(self, cog, currency):
        super().__init__(title=f"{currency} payout address")
        self.cog = cog
        self.currency = currency
        self.address = discord.ui.TextInput(
            label=f"Your {currency} address", placeholder="Where withdrawals go"
        )
        self.add_item(self.address)

    async def on_submit(self, interaction):
        addr = self.address.value.strip()
        chain = self.cog.wallets.chain(self.currency)
        if not chain.validate_address(addr):
            await interaction.response.send_message(
                f"That doesn't look like a valid {self.currency} address.", ephemeral=True
            )
            return
        await self.cog.bot.db.set_payout_address(self.currency, addr)
        await interaction.response.send_message(
            f"\u2705 {self.currency} payout address set to `{addr}`.", ephemeral=True
        )


class ThresholdModal(discord.ui.Modal):
    def __init__(self, cog):
        super().__init__(title="Auto-withdraw threshold (USD)")
        self.cog = cog
        self.amount = discord.ui.TextInput(
            label="Sweep a coin when its balance reaches ($)",
            placeholder="e.g. 200 — use 0 to disable",
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction):
        try:
            usd = float(self.amount.value.strip().lstrip("$"))
            if usd < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a number like `200`.", ephemeral=True)
            return
        for currency in CURRENCIES:
            await self.cog.bot.db.set_auto_threshold(currency, usd)
        state = f"sweep at {fmt_usd(usd)}" if usd else "disabled"
        await interaction.response.send_message(
            f"\u2705 Auto-withdraw {state} (applies to every coin).", ephemeral=True
        )


class WalletAdminView(discord.ui.View):
    def __init__(self, cog, owner_id):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id

        self.address_select = discord.ui.Select(
            placeholder="Set payout address for…",
            options=[discord.SelectOption(label=c, value=c) for c in CURRENCIES],
            row=0,
        )
        self.address_select.callback = self._on_set_address
        self.add_item(self.address_select)

        self.withdraw_select = discord.ui.Select(
            placeholder="Withdraw (sweep) now…",
            options=[discord.SelectOption(label=f"Withdraw all {c}", value=c) for c in CURRENCIES],
            row=1,
        )
        self.withdraw_select.callback = self._on_withdraw
        self.add_item(self.withdraw_select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("These controls aren't yours.", ephemeral=True)
            return False
        return True

    async def _on_set_address(self, interaction):
        await interaction.response.send_modal(
            PayoutAddressModal(self.cog, self.address_select.values[0])
        )

    async def _on_withdraw(self, interaction):
        currency = self.withdraw_select.values[0]
        settings = await self.cog.bot.db.get_wallet_settings()
        address = (settings.get(currency) or {}).get("payout_address")
        if not address:
            await interaction.response.send_message(
                f"Set a {currency} payout address first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        chain = self.cog.wallets.chain(currency)
        try:
            txid = await chain.sweep(address)
        except ChainError as exc:
            await interaction.followup.send(f"Withdraw failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"\u2705 Swept the whole {currency} balance to `{address}`.\nTx: `{txid}`",
            ephemeral=True,
        )

    @discord.ui.button(label="Auto-withdraw threshold", emoji="\u2699\uFE0F",
                       style=discord.ButtonStyle.secondary, row=2)
    async def threshold_btn(self, interaction, button):
        await interaction.response.send_modal(ThresholdModal(self.cog))

    @discord.ui.button(label="Refresh", emoji="\U0001F504", style=discord.ButtonStyle.primary, row=2)
    async def refresh_btn(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await wallet_admin_embed(self.cog)
        await interaction.followup.send(
            embed=embed, view=WalletAdminView(self.cog, self.owner_id), ephemeral=True
        )


async def wallet_admin_embed(cog):
    settings = await cog.bot.db.get_wallet_settings()
    try:
        prices = await cog.wallets.prices()
    except ChainError:
        prices = {}
    lines = []
    total_usd = 0.0
    for currency in CURRENCIES:
        chain = cog.wallets.chain(currency)
        try:
            bal = await chain.balance()
        except Exception:  # noqa: BLE001
            lines.append(f"**{currency}** — balance unavailable")
            continue
        price = prices.get(currency)
        usd = bal / 10 ** chain.decimals * price if price else None
        if usd is not None:
            total_usd += usd
        s = settings.get(currency) or {}
        payout = s.get("payout_address")
        lines.append(
            f"**{currency}** `{chain.format_amount(bal)}`"
            + (f" ({fmt_usd(usd)})" if usd is not None else "")
            + f"\n\u2514 deposit: `{chain.address}`"
            + (f"\n\u2514 payout: `{payout}`" if payout else "\n\u2514 payout: *not set*")
        )
    thresholds = {
        (settings.get(c) or {}).get("auto_threshold_usd") or 0 for c in CURRENCIES
    }
    thr = max(thresholds) if thresholds else 0
    embed = discord.Embed(
        title="\U0001F3E6 Hot wallet",
        color=config.EMBED_COLOR,
        description="\n\n".join(lines),
    )
    embed.add_field(name="Total value", value=fmt_usd(total_usd), inline=True)
    embed.add_field(
        name="Auto-withdraw",
        value=f"at {fmt_usd(thr)} per coin" if thr else "off",
        inline=True,
    )
    revenue = await cog.bot.db.topup_revenue()
    if revenue:
        rev_lines = [
            f"{row['currency']}: {row['n']} top-up(s), {fmt_usd(row['usd'] or 0)}"
            for row in revenue
        ]
        embed.add_field(name="Revenue (paid top-ups)", value="\n".join(rev_lines), inline=False)
    embed.set_footer(text="Balances are live on-chain values")
    return embed


async def setup(bot):
    await bot.add_cog(PaymentsCog(bot))
