"""
Ethereum Transaction Monitor - Telegram Bot
Monitors the Ethereum blockchain and alerts on large transactions.
Compatible with: web3>=7.0, python-telegram-bot>=21.5, Python 3.11+
"""

import asyncio
import atexit
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
from telegram import Update, LinkPreviewOptions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)
from web3 import AsyncWeb3
from web3.providers.persistent import WebSocketProvider

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WEB3_WSS_ENDPOINT,
    DEFAULT_THRESHOLD_USD,
    COINGECKO_API_URL,
    ETH_PRICE_CACHE_SECONDS,
    LOG_LEVEL,
    LOG_FILE,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("eth_monitor")


# ── Single-Instance Lock ───────────────────────────────────────────────────────
_LOCK_FILE = "/tmp/eth_monitor.lock"

def _acquire_lock() -> None:
    """Prevent multiple bot instances (causes Telegram 409 Conflict)."""
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            print(f"ERROR: Another instance is already running (PID {old_pid}).")
            print("Run:  pkill -f bot.py   then try again.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            os.remove(_LOCK_FILE)

    with open(_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(_LOCK_FILE) and os.remove(_LOCK_FILE))
    logger.info("Lock acquired (PID %d).", os.getpid())


# ── Conversation states ────────────────────────────────────────────────────────
AWAITING_WALLET_ADDRESS = 1
AWAITING_WALLET_LABEL   = 2


# ── State ──────────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.monitoring: bool = False
        # Range filter: alert when min_usd <= value <= max_usd
        # max_usd of None means no upper limit
        self.min_usd: float = DEFAULT_THRESHOLD_USD
        self.max_usd: Optional[float] = None
        self.monitor_task: Optional[asyncio.Task] = None
        self.eth_price_usd: float = 0.0
        self.eth_price_updated: float = 0.0
        self.blocks_scanned: int = 0
        self.alerts_sent: int = 0
        self.start_time: float = 0.0
        # wallet watchlist: { "0xabcd...": "My Label" }  (keys always lowercase)
        self.watched_wallets: dict[str, str] = {}

    def in_range(self, usd_value: float) -> bool:
        """Return True if usd_value falls within the configured alert range."""
        if usd_value < self.min_usd:
            return False
        if self.max_usd is not None and usd_value > self.max_usd:
            return False
        return True

    def range_display(self) -> str:
        """Human-readable range string."""
        lo = f"${self.min_usd:,.0f}"
        hi = f"${self.max_usd:,.0f}" if self.max_usd is not None else "\u221e"
        return f"{lo} \u2013 {hi}"


state = BotState()


# ── Wallet helpers ─────────────────────────────────────────────────────────────
ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

def is_valid_eth_address(addr: str) -> bool:
    return bool(ETH_ADDRESS_RE.match(addr))

def normalise_address(addr: str) -> str:
    """Lowercase for consistent dict keying."""
    return addr.lower()

def wallet_display(addr: str) -> str:
    """Shortened address: 0x1234...abcd"""
    return f"{addr[:6]}...{addr[-4:]}"


# ── ETH Price ──────────────────────────────────────────────────────────────────
async def fetch_eth_price() -> float:
    now = time.time()
    if now - state.eth_price_updated < ETH_PRICE_CACHE_SECONDS and state.eth_price_usd:
        return state.eth_price_usd
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                COINGECKO_API_URL,
                params={"ids": "ethereum", "vs_currencies": "usd"},
                timeout=10,
            )
            resp.raise_for_status()
            price = float(resp.json()["ethereum"]["usd"])
            state.eth_price_usd = price
            state.eth_price_updated = now
            logger.debug("ETH price refreshed: $%.2f", price)
            return price
    except Exception as exc:
        logger.warning("Could not fetch ETH price: %s", exc)
        return state.eth_price_usd or 0.0


def fetch_eth_price_sync() -> float:
    now = time.time()
    if now - state.eth_price_updated < ETH_PRICE_CACHE_SECONDS and state.eth_price_usd:
        return state.eth_price_usd
    try:
        import requests
        resp = requests.get(
            COINGECKO_API_URL,
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        price = float(resp.json()["ethereum"]["usd"])
        state.eth_price_usd = price
        state.eth_price_updated = now
        return price
    except Exception as exc:
        logger.warning("Could not fetch ETH price (sync): %s", exc)
        return state.eth_price_usd or 0.0


# ── Alert Formatter ────────────────────────────────────────────────────────────
def build_alert(
    tx: dict,
    eth_amount: Decimal,
    usd_value: float,
    block_timestamp: Optional[int] = None,
    watched_sender: Optional[str] = None,
    watched_receiver: Optional[str] = None,
) -> str:
    tx_hash  = tx["hash"].hex() if isinstance(tx["hash"], bytes) else tx["hash"]
    sender   = tx.get("from") or "Unknown"
    receiver = tx.get("to")   or "Contract Creation"

    sender_tag = receiver_tag = ""
    if watched_sender:
        label = state.watched_wallets.get(normalise_address(sender), "")
        sender_tag = f"  👀 WATCHED{' — ' + label if label else ''}"
    if watched_receiver:
        label = state.watched_wallets.get(normalise_address(receiver), "")
        receiver_tag = f"  👀 WATCHED{' — ' + label if label else ''}"

    header = (
        "👀 Watched Wallet Alert"
        if (watched_sender or watched_receiver)
        else "🚨 Large Transaction Detected"
    )

    # Format block timestamp (UTC)
    if block_timestamp:
        dt = datetime.fromtimestamp(block_timestamp, tz=timezone.utc)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        time_str = "Unknown"

    return (
        f"{header}\n\n"
        f"🕐 Time:  {time_str}\n"
        f"Value: ${usd_value:,.2f}  |  Amount: {eth_amount:.6f} ETH\n"
        f"From: `{sender}`{sender_tag}\n"
        f"To:   `{receiver}`{receiver_tag}\n"
        f"Tx Hash: `{tx_hash}`\n"
        f"Etherscan: https://etherscan.io/tx/{tx_hash}"
    )


# ── Blockchain Monitor ─────────────────────────────────────────────────────────
async def monitor_blockchain(app: Application) -> None:
    reconnect_delay = 5

    while state.monitoring:
        try:
            logger.info("Connecting to Ethereum node...")

            async with AsyncWeb3(WebSocketProvider(WEB3_WSS_ENDPOINT)) as w3:
                if not await w3.is_connected():
                    raise ConnectionError("Web3 provider failed to connect.")

                chain_id = await w3.eth.chain_id
                logger.info("Connected. Chain ID: %d", chain_id)
                reconnect_delay = 5

                subscription_id = await w3.eth.subscribe("newHeads")
                logger.info("Subscribed to newHeads. Sub ID: %s", subscription_id)

                async for response in w3.socket.process_subscriptions():
                    if not state.monitoring:
                        await w3.eth.unsubscribe(subscription_id)
                        break

                    if isinstance(response, dict) and "result" in response:
                        block_header = response["result"]
                    else:
                        block_header = response

                    raw_number = block_header.get("number")
                    if raw_number is None:
                        logger.debug("Response with no block number, skipping: %s", response)
                        continue

                    block_number = int(raw_number, 16) if isinstance(raw_number, str) else int(raw_number)
                    logger.info("New block: %d", block_number)

                    try:
                        block = await w3.eth.get_block(block_number, full_transactions=True)
                    except Exception as e:
                        logger.warning("Could not fetch block %d: %s", block_number, e)
                        continue

                    eth_price = await fetch_eth_price()
                    if eth_price == 0:
                        logger.warning("ETH price unavailable; skipping block %d.", block_number)
                        continue

                    state.blocks_scanned += 1
                    logger.debug("Scanning %d txs in block %d", len(block.transactions), block_number)

                    watched_set = set(state.watched_wallets.keys())  # fast O(1) lookup

                    for tx in block.transactions:
                        try:
                            eth_amount = Decimal(tx["value"]) / Decimal(10**18)
                            usd_value  = float(eth_amount) * eth_price

                            sender_norm   = normalise_address(tx.get("from") or "")
                            receiver_norm = normalise_address(tx.get("to")   or "")

                            is_watched_sender   = sender_norm   in watched_set
                            is_watched_receiver = receiver_norm in watched_set
                            is_large            = state.in_range(usd_value)

                            if is_large or is_watched_sender or is_watched_receiver:
                                block_ts = getattr(block, "timestamp", None)
                                alert_text = build_alert(
                                    tx, eth_amount, usd_value,
                                    block_timestamp  = block_ts,
                                    watched_sender   = sender_norm   if is_watched_sender   else None,
                                    watched_receiver = receiver_norm if is_watched_receiver else None,
                                )
                                await app.bot.send_message(
                                    chat_id=TELEGRAM_CHAT_ID,
                                    text=alert_text,
                                    parse_mode="Markdown",
                                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                                )
                                state.alerts_sent += 1
                                tx_hash = tx["hash"].hex() if isinstance(tx["hash"], bytes) else tx["hash"]
                                logger.info("Alert sent -- $%.2f | tx %s...", usd_value, tx_hash[:16])
                        except Exception as tx_err:
                            logger.warning("Error processing tx: %s", tx_err)

        except asyncio.CancelledError:
            logger.info("Monitor task cancelled.")
            break
        except Exception as exc:
            if not state.monitoring:
                break
            logger.error("Monitor error: %s -- reconnecting in %ds", exc, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 120)

    logger.info("Monitoring stopped.")


# ── /wallet — show watchlist menu ─────────────────────────────────────────────
async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the wallet watchlist with interactive Add / Remove buttons."""
    wallets = state.watched_wallets
    if wallets:
        lines = ["👛 *Watched Wallets*\n"]
        for addr, label in wallets.items():
            tag = f" — _{label}_" if label else ""
            lines.append(f"• `{addr}`{tag}")
        text = "\n".join(lines)
    else:
        text = "👛 *Watched Wallets*\n\nNo wallets are being watched yet."

    keyboard = [
        [
            InlineKeyboardButton("➕ Add Wallet",    callback_data="wallet_add"),
            InlineKeyboardButton("➖ Remove Wallet", callback_data="wallet_remove"),
        ],
        [InlineKeyboardButton("🗑 Clear All", callback_data="wallet_clear")],
    ]
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Conversation: interactive Add Wallet flow ──────────────────────────────────
async def wallet_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the Add / Remove / Clear buttons on the wallet menu."""
    query = update.callback_query
    await query.answer()

    if query.data == "wallet_add":
        await query.message.reply_text(
            "📋 *Add a Wallet*\n\n"
            "Paste the Ethereum address you want to watch\n"
            "(e.g. `0xAbCd...1234`)\n\n"
            "Send /cancel to abort.",
            parse_mode="Markdown",
        )
        return AWAITING_WALLET_ADDRESS

    if query.data == "wallet_remove":
        wallets = state.watched_wallets
        if not wallets:
            await query.message.reply_text("No wallets to remove.")
            return ConversationHandler.END
        buttons = [
            [InlineKeyboardButton(
                f"{wallet_display(addr)}{' — ' + lbl if lbl else ''}",
                callback_data=f"remove_{addr}",
            )]
            for addr, lbl in wallets.items()
        ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_remove")])
        await query.message.reply_text(
            "Select a wallet to remove:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return ConversationHandler.END

    if query.data == "wallet_clear":
        count = len(state.watched_wallets)
        state.watched_wallets.clear()
        await query.message.reply_text(f"🗑 Cleared {count} wallet(s) from the watchlist.")
        return ConversationHandler.END

    return ConversationHandler.END


async def receive_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    addr = update.message.text.strip()

    if not is_valid_eth_address(addr):
        await update.message.reply_text(
            "❌ That doesn't look like a valid Ethereum address.\n"
            "It must start with `0x` and be 42 characters long.\n\n"
            "Try again or send /cancel.",
            parse_mode="Markdown",
        )
        return AWAITING_WALLET_ADDRESS  # let them retry

    norm = normalise_address(addr)
    if norm in state.watched_wallets:
        await update.message.reply_text(
            f"⚠️ `{addr}` is already in your watchlist.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    context.user_data["pending_wallet"] = norm
    await update.message.reply_text(
        f"✅ Address: `{addr}`\n\n"
        f"Send a *label* for this wallet (e.g. `Binance Hot Wallet`),\n"
        f"or send `-` to skip.",
        parse_mode="Markdown",
    )
    return AWAITING_WALLET_LABEL


async def receive_wallet_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    label = update.message.text.strip()
    if label == "-":
        label = ""

    addr = context.user_data.pop("pending_wallet", None)
    if not addr:
        await update.message.reply_text("Something went wrong. Please try /wallet again.")
        return ConversationHandler.END

    state.watched_wallets[addr] = label
    display_label = f" as *{label}*" if label else ""
    await update.message.reply_text(
        f"👀 Now watching `{addr}`{display_label}.\n\n"
        f"You'll be alerted on *any* transaction involving this wallet, "
        f"regardless of size.",
        parse_mode="Markdown",
    )
    logger.info("Wallet added: %s (%s)", addr, label or "no label")
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pending_wallet", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── Inline remove-wallet button handler ───────────────────────────────────────
async def remove_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_remove":
        await query.message.reply_text("Cancelled.")
        return

    if query.data.startswith("remove_"):
        addr = query.data[len("remove_"):]
        label = state.watched_wallets.pop(addr, None)
        if label is not None:
            suffix = f" (*{label}*)" if label else ""
            await query.message.reply_text(
                f"✅ Removed `{addr}`{suffix} from watchlist.",
                parse_mode="Markdown",
            )
            logger.info("Wallet removed: %s", addr)
        else:
            await query.message.reply_text("Wallet not found (already removed?).")


# ── /addwallet shortcut ────────────────────────────────────────────────────────
async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /addwallet 0xAddress [optional label]
    If no address is given, starts the guided conversation flow.
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "📋 *Add a Wallet*\n\n"
            "Paste the Ethereum address you want to watch.\n"
            "Send /cancel to abort.",
            parse_mode="Markdown",
        )
        return AWAITING_WALLET_ADDRESS

    addr = args[0].strip()
    if not is_valid_eth_address(addr):
        await update.message.reply_text(
            "❌ Invalid address. Example:\n`/addwallet 0xAbCd...1234 Binance`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    norm  = normalise_address(addr)
    label = " ".join(args[1:]) if len(args) > 1 else ""

    if norm in state.watched_wallets:
        await update.message.reply_text(
            f"⚠️ `{addr}` is already being watched.", parse_mode="Markdown"
        )
        return ConversationHandler.END

    state.watched_wallets[norm] = label
    display_label = f" as *{label}*" if label else ""
    await update.message.reply_text(
        f"👀 Now watching `{addr}`{display_label}.", parse_mode="Markdown"
    )
    logger.info("Wallet added via /addwallet: %s (%s)", norm, label or "no label")
    return ConversationHandler.END


# ── /removewallet shortcut ─────────────────────────────────────────────────────
async def cmd_removewallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removewallet 0xAddress"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/removewallet 0xAddress`", parse_mode="Markdown"
        )
        return
    norm  = normalise_address(args[0].strip())
    label = state.watched_wallets.pop(norm, None)
    if label is not None:
        await update.message.reply_text(
            f"✅ Removed `{norm}` from watchlist.", parse_mode="Markdown"
        )
        logger.info("Wallet removed via /removewallet: %s", norm)
    else:
        await update.message.reply_text(
            f"⚠️ `{norm}` was not in the watchlist.", parse_mode="Markdown"
        )


# ── Standard commands ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if state.monitoring:
        await update.message.reply_text("Already running.")
        return

    state.monitoring     = True
    state.start_time     = time.time()
    state.blocks_scanned = 0
    state.alerts_sent    = 0
    state.monitor_task   = asyncio.create_task(monitor_blockchain(context.application))

    await update.message.reply_text(
        f"✅ Ethereum monitor started!\n"
        f"Alert range: {state.range_display()} USD\n\n"
        f"Commands:\n"
        f"/stop — halt monitoring\n"
        f"/range — set alert value range\n"
        f"/wallet — manage watched wallets\n"
        f"/status — show stats"
    )
    logger.info("Monitoring started by user %s.", update.effective_user.id)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not state.monitoring:
        await update.message.reply_text("Monitor is not running.")
        return

    state.monitoring = False
    if state.monitor_task:
        state.monitor_task.cancel()
        try:
            await state.monitor_task
        except asyncio.CancelledError:
            pass
        state.monitor_task = None

    await update.message.reply_text("🛑 Monitoring stopped.")
    logger.info("Monitoring stopped by user %s.", update.effective_user.id)


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kept as a backwards-compatible alias for /range with a single minimum value."""
    args = context.args
    if not args:
        await update.message.reply_text(
            f"Current range: {state.range_display()} USD\n\n"
            f"To set a minimum: /threshold 10000\n"
            f"For a full range use: /range 10000 50000"
        )
        return
    try:
        new_val = float(args[0].replace(",", "").replace("$", ""))
        if new_val < 0:
            raise ValueError
        state.min_usd = new_val
        state.max_usd = None
        await update.message.reply_text(
            f"✅ Threshold set to ${state.min_usd:,.0f}+ USD\n"
            f"Tip: use /range to set an upper limit too."
        )
        logger.info("Min threshold set to $%.2f by user %s.", new_val, update.effective_user.id)
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid value. Example: /threshold 10000")


async def cmd_range(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Set a USD value range for alerts.

    Usage:
      /range              — show current range
      /range 5000         — alert on txs >= $5,000 (no upper limit)
      /range 5000 50000   — alert on txs between $5,000 and $50,000
      /range 0 1000       — alert on txs up to $1,000 (e.g. small tx watch)
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            f"📊 *Alert Range*\n\n"
            f"Current range: *{state.range_display()} USD*\n\n"
            f"Usage:\n"
            f"`/range 5000`          — $5,000 and above\n"
            f"`/range 5000 50000`    — $5,000 to $50,000\n"
            f"`/range 0 1000`        — up to $1,000\n"
            f"`/range 0`             — alert on every transaction",
            parse_mode="Markdown",
        )
        return

    try:
        min_val = float(args[0].replace(",", "").replace("$", ""))
        if min_val < 0:
            raise ValueError("min cannot be negative")

        max_val: Optional[float] = None
        if len(args) >= 2:
            max_val = float(args[1].replace(",", "").replace("$", ""))
            if max_val < 0:
                raise ValueError("max cannot be negative")
            if max_val < min_val:
                await update.message.reply_text(
                    "❌ Upper limit must be greater than or equal to the lower limit.\n"
                    "Example: `/range 5000 50000`",
                    parse_mode="Markdown",
                )
                return

        state.min_usd = min_val
        state.max_usd = max_val

        hi_str = f"${max_val:,.0f}" if max_val is not None else "∞"
        await update.message.reply_text(
            f"✅ Alert range updated: *${min_val:,.0f} – {hi_str} USD*",
            parse_mode="Markdown",
        )
        logger.info(
            "Range set to $%.2f–%s by user %s.",
            min_val,
            f"${max_val:,.2f}" if max_val else "∞",
            update.effective_user.id,
        )
    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            f"❌ Invalid value: {e}\nExample: `/range 5000 50000`",
            parse_mode="Markdown",
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status    = "🟢 Running" if state.monitoring else "🔴 Stopped"
    eth_price = fetch_eth_price_sync()
    uptime    = "--"
    if state.monitoring and state.start_time:
        e = int(time.time() - state.start_time)
        uptime = f"{e//3600:02d}:{(e%3600)//60:02d}:{e%60:02d}"

    await update.message.reply_text(
        f"📊 Bot Status\n\n"
        f"Status:          {status}\n"
        f"Alert Range:     {state.range_display()} USD\n"
        f"ETH Price:       ${eth_price:,.2f} USD\n"
        f"Blocks Scanned:  {state.blocks_scanned:,}\n"
        f"Alerts Sent:     {state.alerts_sent:,}\n"
        f"Watched Wallets: {len(state.watched_wallets)}\n"
        f"Uptime:          {uptime}"
    )


# ── Entry Point ────────────────────────────────────────────────────────────────
def main() -> None:
    _acquire_lock()
    logger.info("Starting Ethereum Monitor Bot...")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ConversationHandler covers both the guided /addwallet flow
    # and the "Add Wallet" button on the /wallet menu
    wallet_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addwallet", cmd_addwallet),
            CallbackQueryHandler(wallet_menu_callback, pattern="^wallet_add$"),
        ],
        states={
            AWAITING_WALLET_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_wallet_address),
            ],
            AWAITING_WALLET_LABEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_wallet_label),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )

    app.add_handler(wallet_conv)

    # Remaining wallet-menu button callbacks (remove/clear handled outside conversation)
    app.add_handler(CallbackQueryHandler(wallet_menu_callback,   pattern="^wallet_(remove|clear)$"))
    app.add_handler(CallbackQueryHandler(remove_wallet_callback, pattern="^(remove_|cancel_remove)"))

    # Standard commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("stop",         cmd_stop))
    app.add_handler(CommandHandler("threshold",    cmd_threshold))   # backwards-compat alias
    app.add_handler(CommandHandler("range",        cmd_range))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("wallet",       cmd_wallet))
    app.add_handler(CommandHandler("removewallet", cmd_removewallet))

    logger.info("Bot is polling for commands.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
