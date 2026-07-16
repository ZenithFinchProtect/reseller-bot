# Nordic Reseller Bot

A Discord bot that gives resellers self-serve tools against the NFA API, and
lets each reseller server subscribe its own channel to recurring stock updates.

## Commands

- `/stock` - live stock snapshot (counts capped, default 5)
- `/test` - health check: bot latency, NFA API reachability, and (if set) posts a
  test update to this server's webhook
- `/stock-url <webhook_url>` - paste a Discord webhook URL and recurring stock
  updates post there (requires **Manage Server**). Create the webhook in
  *Server Settings → Integrations → Webhooks → Copy Webhook URL*, then paste it.
  A first update is sent immediately to confirm it works.
- `/webhook-settings` - interactive panel to customise the updates for this
  server: which **games** show, the display **cap**, **show/hide out-of-stock**
  rows, and **how often** updates send. Also has a "Send update now" button.
- `/stock-stop` - stop updates for this server
- `/check <key>` - re-validate an activated key
- `/replace <key>` - replace an invalid key within the 3-hour warranty
- `/delete <key>` - delete an unactivated key (removes it from stock)
- `/buy` - placeholder; balances + checkout arrive with the reseller website

Each server's updates use its own saved settings. Defaults come from
`STOCK_UPDATE_MINUTES` (default 30) and `STOCK_CAP` (default 5, matching the
main-site embeds) until changed via `/webhook-settings`. A background tick checks
every minute and sends to each server on its own schedule.

## Setup

1. Create a bot application at https://discord.com/developers/applications
   (Bot → Reset Token). Invite it with scopes `bot` + `applications.commands`
   and permission `Send Messages`.
2. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `NFA_API_KEY`, and
   (recommended) `GUILD_ID`.
3. Run:

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## Deploy (Railway)

Uses the included `Dockerfile` / `railway.json`. Set the environment variables
(`DISCORD_TOKEN`, `NFA_API_KEY`, `GUILD_ID`, ...) in the Railway project — never
bake secrets into the image. SQLite (`reseller_data.db`) stores the per-server
webhook subscriptions; mount a volume if you want them to survive redeploys.

## Crypto top-ups (hot wallet)

Members can buy coins with **BTC / ETH / SOL / LTC** from the `/coin` hub's
**Top Up** button. Payments go straight to a hot wallet the bot controls:

1. Start the bot once without `HOT_WALLET_MNEMONIC` set — it logs a freshly
   generated 12-word seed. Save it somewhere safe (it *is* the money) and set
   it as the `HOT_WALLET_MNEMONIC` env var, then restart.
2. Each top-up invoice shows the deposit address plus a unique exact amount;
   the bot watches free public explorers (mempool.space, litecoinspace.org,
   Blockscout, Solana RPC) and credits coins after 1 confirmation, then DMs
   the buyer. Invoices expire after `TOPUP_EXPIRE_MINUTES` (default 60).
3. Admins run `/coin-admin wallet` for live balances, per-coin payout
   addresses, a manual **Withdraw** (sweeps the full balance in one tx), and
   an **auto-withdraw** USD threshold that sweeps automatically.
4. Override packages with `TOPUP_PACKAGES`, e.g.
   `[{"coins": 1, "usd": 2}, {"coins": 7, "usd": 10}]`.

Treat the hot wallet as a working float: set a payout address and sweep
regularly. No third party, no fees beyond normal network fees, and the seed
never leaves your environment variables.

## Notes

- `/buy` is intentionally a no-op for now. Reseller balances and purchasing will
  be added once the reseller website (top-up + checkout) is built.
- The bot only talks to the NFA API with the server-side `NFA_API_KEY`; that key
  is never exposed to users.
