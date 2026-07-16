"""Configuration loader for the Nordic reseller bot.

Reads from environment / .env. Most values are defaults; the per-server stock
webhook subscriptions live in the database (see db.py).
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(name, default=None):
    raw = os.getenv(name)
    if raw and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return default


def _get_id_set(name, default):
    raw = os.getenv(name)
    if not raw:
        return set(default)
    ids = {int(p) for p in raw.replace(",", " ").split() if p.strip().isdigit()}
    return ids or set(default)


# --- Required ---
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Optional: set to your server (guild) ID for INSTANT slash-command updates.
# Leave blank to register commands globally (can take up to ~1 hour the first time).
GUILD_ID = _get_int("GUILD_ID")

# Users who may run admin-only actions (currently none required, reserved).
ADMIN_USER_IDS = _get_id_set("ADMIN_USER_IDS", [])

# --- NFA Resell API ---
# Set NFA_API_KEY as an environment variable / Railway secret. Never hardcode it.
NFA_API_KEY = os.getenv("NFA_API_KEY", "").strip()
NFA_API_BASE = os.getenv("NFA_API_BASE", "https://nfa-api.acode.ing").rstrip("/")

# Public activation site customers use to redeem keys.
ACTIVATION_URL = os.getenv("ACTIVATION_URL", "https://nordicnfas.com/")

# --- Stock display ---
# Cap the displayed stock count (matches the main-site embeds). 0 disables the cap.
STOCK_CAP = _get_int("STOCK_CAP", 5)
# How often (minutes) registered server webhooks receive a stock update.
STOCK_UPDATE_MINUTES = _get_int("STOCK_UPDATE_MINUTES", 30)

# --- Cosmetic ---
EMBED_COLOR = int(os.getenv("EMBED_COLOR", "0x33C2FF"), 16)  # icy blue
STOCK_EMBED_TITLE = os.getenv("STOCK_EMBED_TITLE", "Nordic Stock Update")
# Username/avatar used when posting to a reseller's webhook.
WEBHOOK_USERNAME = os.getenv("WEBHOOK_USERNAME", "Nordic Stock")
WEBHOOK_AVATAR_URL = os.getenv("WEBHOOK_AVATAR_URL", "").strip()

# --- Internals ---
# Store the SQLite DB on the Railway volume when one is mounted so coin
# balances survive restarts and redeploys.
_VOLUME = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
_DEFAULT_DB = os.path.join(_VOLUME, "reseller_data.db") if _VOLUME else "reseller_data.db"
DB_PATH = os.getenv("DB_PATH", _DEFAULT_DB)

# The bot's own displayed status text.
BOT_STATUS_TEXT = os.getenv("BOT_STATUS_TEXT", "nordicnfas.com")

# --- Coins (status rewards + /coin hub) ---
# Members earn coins for time spent online while displaying the required
# custom-status text. Defaults are per-server tunable via /coin-admin.
DEFAULT_REQUIRED_STATUS = os.getenv("REQUIRED_STATUS", "$1.20 Rust: nfaccount.com")
DEFAULT_REWARD_HOURS = float(os.getenv("REWARD_HOURS", "48"))
DEFAULT_COINS_PER_REWARD = _get_int("COINS_PER_REWARD", 1)
DEFAULT_LOG_CHANNEL_ID = _get_int("LOG_CHANNEL_ID")
DEFAULT_ELIGIBLE_STATUSES = os.getenv("ELIGIBLE_STATUSES", "online,dnd")
COIN_NAME = os.getenv("COIN_NAME", "coin")
COIN_EMOJI = os.getenv("COIN_EMOJI", "\U0001FA99")  # 🪙
# How often (seconds) the bot credits online time and checks for rewards.
TICK_SECONDS = _get_int("TICK_SECONDS", 60)

# --- Coin HTTP API (used by the shop bot and the casino site) ---
# Shared key clients must send in X-API-Key; unset disables the API.
CASINO_API_KEY = os.getenv("CASINO_API_KEY", "").strip()
# Guild whose balances/settings the API exposes; defaults to GUILD_ID.
CASINO_API_GUILD_ID = _get_int("CASINO_API_GUILD_ID", GUILD_ID)
# Port the shared web server (relay + coin API) listens on.
WEB_PORT = _get_int("PORT", _get_int("RELAY_PORT", 8080))

# --- Gambling (mirrors the casino site's odds) ---
GAMBLE_WIN_CHANCE = float(os.getenv("GAMBLE_WIN_CHANCE", "0.34"))
GAMBLE_MULTIPLIER = float(os.getenv("GAMBLE_MULTIPLIER", "2"))
GAMBLE_MAX_BET = _get_int("GAMBLE_MAX_BET", 5)

# --- Crypto top-ups (hot wallet) ---
# BIP39 seed of the receiving hot wallet. Leave unset to disable top-ups;
# the bot logs a freshly generated mnemonic on boot so you can copy it into
# the environment (never commit it anywhere).
HOT_WALLET_MNEMONIC = os.getenv("HOT_WALLET_MNEMONIC", "").strip()
# Minutes a top-up invoice stays valid.
TOPUP_EXPIRE_MINUTES = _get_int("TOPUP_EXPIRE_MINUTES", 60)
# Seconds between blockchain checks for pending invoices.
TOPUP_POLL_SECONDS = _get_int("TOPUP_POLL_SECONDS", 60)
# Seconds between auto-withdraw balance checks.
AUTO_WITHDRAW_POLL_SECONDS = _get_int("AUTO_WITHDRAW_POLL_SECONDS", 600)
# Packages members can buy: coins granted per USD price.
DEFAULT_TOPUP_PACKAGES = [
    {"coins": 1, "usd": 2.0},
    {"coins": 3, "usd": 5.0},
    {"coins": 7, "usd": 10.0},
    {"coins": 15, "usd": 20.0},
    {"coins": 40, "usd": 50.0},
]
try:
    TOPUP_PACKAGES = json.loads(os.getenv("TOPUP_PACKAGES", "")) or DEFAULT_TOPUP_PACKAGES
except (ValueError, TypeError):
    TOPUP_PACKAGES = DEFAULT_TOPUP_PACKAGES

# --- Store products (priced in coins) ---
# Override with a STORE_TIERS env var (JSON list) if you want to change them.
DEFAULT_STORE_TIERS = [
    {"account_type": "rust_0_250_hours", "label": "Rust 0-250 hours (Base)", "cost": 1},
    {"account_type": "rust_500_1000_hours", "label": "Rust 500-1000 hours", "cost": 3},
    {"account_type": "rust_3000_7000_hours", "label": "Rust 3000-7000 hours", "cost": 4},
    {"account_type": "arc_0_99_hours", "label": "Arc 0-99 hours", "cost": 1},
    {"account_type": "arc_100_200_hours", "label": "Arc 100-200 hours", "cost": 3},
    {"account_type": "arc_200_plus_hours", "label": "Arc 200+ hours", "cost": 5},
    {"account_type": "cs2_prime", "label": "CS2 Prime", "cost": 1},
    {"account_type": "cs2_premier", "label": "CS2 Premier", "cost": 1},
    {"account_type": "cs2_10_15k_elo", "label": "CS2 10-15k ELO", "cost": 2},
    {"account_type": "cs2_15_20k_elo", "label": "CS2 15-20k ELO", "cost": 3},
]
try:
    STORE_TIERS = json.loads(os.getenv("STORE_TIERS", "")) or DEFAULT_STORE_TIERS
except (ValueError, TypeError):
    STORE_TIERS = DEFAULT_STORE_TIERS
