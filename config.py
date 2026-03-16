"""
config.py — Central configuration for the Ethereum Monitor Bot.

Fill in your credentials below, or set them as environment variables
(env vars take precedence over the hard-coded defaults).
"""

import os

# ── Telegram ───────────────────────────────────────────────────────────────────
# Get these from @BotFather (token) and your own account/group (chat_id).
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_TELEGRAM_CHAT_ID_HERE")

# ── Ethereum Node (WebSocket) ──────────────────────────────────────────────────
# Infura:  wss://mainnet.infura.io/ws/v3/YOUR_PROJECT_ID
# Alchemy: wss://eth-mainnet.g.alchemy.com/v2/YOUR_API_KEY
WEB3_WSS_ENDPOINT: str = os.getenv(
    "WEB3_WSS_ENDPOINT",
    "wss://mainnet.infura.io/ws/v3/YOUR_INFURA_PROJECT_ID",
)

# ── Alert Threshold ────────────────────────────────────────────────────────────
# Default minimum USD value that triggers an alert.
DEFAULT_THRESHOLD_USD: float = float(os.getenv("DEFAULT_THRESHOLD_USD", "5000"))

# ── CoinGecko ──────────────────────────────────────────────────────────────────
COINGECKO_API_URL: str = "https://api.coingecko.com/api/v3/simple/price"

# How long (seconds) to cache the ETH price before re-fetching.
# Reduces CoinGecko API calls significantly (one call per block is wasteful).
ETH_PRICE_CACHE_SECONDS: int = int(os.getenv("ETH_PRICE_CACHE_SECONDS", "30"))

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")   # DEBUG | INFO | WARNING | ERROR
LOG_FILE:  str = os.getenv("LOG_FILE",  "eth_monitor.log")
