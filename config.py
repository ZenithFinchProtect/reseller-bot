"""Configuration loader for the Nordic reseller bot.

Reads from environment / .env. Most values are defaults; the per-server stock
webhook subscriptions live in the database (see db.py).
"""
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
DB_PATH = os.getenv("DB_PATH", "reseller_data.db")

# The bot's own displayed status text.
BOT_STATUS_TEXT = os.getenv("BOT_STATUS_TEXT", "nordicnfas.com")
