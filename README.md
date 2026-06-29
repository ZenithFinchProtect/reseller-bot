# Nordic Reseller Bot

A Discord bot that gives resellers self-serve tools against the NFA API, and
lets each reseller server subscribe its own channel to recurring stock updates.

## Commands

- `/stock` - live stock snapshot (counts capped, default 5)
- `/stock-url` - the bot **creates a webhook in the current channel** and starts
  posting recurring stock updates there (requires **Manage Server**; the bot
  needs **Manage Webhooks** in that channel). No copy-paste needed.
- `/stock-paste <webhook_url>` - register an existing Discord webhook URL instead
  (for channels where the bot can't create one).
- `/webhook-settings` - interactive panel to customise the updates for this
  server: which **games** show, the display **cap**, **show/hide out-of-stock**
  rows, and **how often** updates send. Also has a "Send update now" button.
- `/stock-stop` - stop updates and delete the managed webhook
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
   and permissions `Send Messages` + `Manage Webhooks` (needed for `/stock-url`).
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

## Notes

- `/buy` is intentionally a no-op for now. Reseller balances and purchasing will
  be added once the reseller website (top-up + checkout) is built.
- The bot only talks to the NFA API with the server-side `NFA_API_KEY`; that key
  is never exposed to users.
