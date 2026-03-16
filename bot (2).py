"""
Ethereum Transaction Monitor - Telegram Bot
Monitors the Ethereum blockchain and alerts on large transactions.
"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from web3 import Web3
from web3.exceptions import BlockNotFound

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


# ── State ──────────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.monitoring: bool = False
        self.threshold_usd: float = DEFAULT_THRESHOLD_USD
        self.monitor_task: Optional[asyncio.Task] = None
        self.eth_price_usd: float = 0.0
        self.eth_price_updated: float = 0.0
        self.blocks_scanned: int = 0
        self.alerts_sent: int = 0
        self.start_time: float = 0.0


state = BotState()


# ── ETH Price ──────────────────────────────────────────────────────────────────
def fetch_eth_price() -> float:
    """Fetch ETH/USD price from CoinGecko, with in-memory caching."""
    now = time.time()
    if now - state.eth_price_updated < ETH_PRICE_CACHE_SECONDS and state.eth_price_usd:
        return state.eth_price_usd

    try:
        resp = requests.get(
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


# ── Alert Formatter ────────────────────────────────────────────────────────────
def build_alert(tx: dict, eth_amount: Decimal, usd_value: float) -> str:
    tx_hash = tx["hash"].hex()
    sender = tx["from"]
    receiver = tx.get("to") or "Contract Creation"
    return (
        f"🚨 *Large Ethereum Transaction Detected*\n\n"
        f"💰 *Value:* ${usd_value:,.2f}  |  *Amount:* {eth_amount:.6f} ETH\n"
        f"📤 *From:* `{sender}`\n"
        f"📥 *To:* `{receiver}`\n"
        f"🔗 *Tx Hash:* `{tx_hash}`\n"
        f"🔍 [View on Etherscan](https://etherscan.io/tx/{tx_hash})"
    )


# ── Blockchain Monitor ─────────────────────────────────────────────────────────
async def monitor_blockchain(app: Application) -> None:
    """
    Core monitoring loop.
    Connects via WebSocket, subscribes to new blocks, scans transactions,
    and sends Telegram alerts. Reconnects automatically on errors.
    """
    reconnect_delay = 5  # seconds, grows on repeated failures

    while state.monitoring:
        w3: Optional[Web3] = None
        try:
            logger.info("Connecting to Ethereum node: %s", WEB3_WSS_ENDPOINT[:40] + "…")
            w3 = Web3(Web3.WebsocketProvider(WEB3_WSS_ENDPOINT, websocket_timeout=60))

            if not w3.is_connected():
                raise ConnectionError("Web3 provider not connected.")

            logger.info("Connected. Chain ID: %d", w3.eth.chain_id)
            reconnect_delay = 5  # reset on successful connect

            # Subscribe to new block headers
            subscription = w3.eth.subscribe("newHeads")  # type: ignore[attr-defined]

            async for block_header in subscription:  # type: ignore[attr-defined]
                if not state.monitoring:
                    break

                block_number = block_header["number"]
                logger.info("New block: %d", block_number)

                try:
                    block = w3.eth.get_block(block_number, full_transactions=True)
                except BlockNotFound:
                    logger.warning("Block %d not found, skipping.", block_number)
                    continue

                eth_price = fetch_eth_price()
                if eth_price == 0:
                    logger.warning("ETH price unavailable; skipping block %d.", block_number)
                    continue

                state.blocks_scanned += 1

                for tx in block.transactions:  # type: ignore[union-attr]
                    try:
                        eth_amount = Decimal(tx["value"]) / Decimal(10**18)
                        usd_value = float(eth_amount) * eth_price

                        if usd_value >= state.threshold_usd:
                            alert_text = build_alert(tx, eth_amount, usd_value)
                            await app.bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=alert_text,
                                parse_mode="Markdown",
                                disable_web_page_preview=True,
                            )
                            state.alerts_sent += 1
                            logger.info(
                                "Alert sent — $%.2f | tx %s",
                                usd_value,
                                tx["hash"].hex()[:16] + "…",
                            )
                    except Exception as tx_err:
                        logger.debug("Error processing tx: %s", tx_err)

        except asyncio.CancelledError:
            logger.info("Monitor task cancelled.")
            break
        except Exception as exc:
            if not state.monitoring:
                break
            logger.error("Monitor error: %s — reconnecting in %ds", exc, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 120)  # exponential back-off, cap 2 min
        finally:
            if w3 and hasattr(w3.provider, "disconnect"):
                try:
                    await w3.provider.disconnect()  # type: ignore[attr-defined]
                except Exception:
                    pass

    logger.info("Monitoring stopped.")


# ── Telegram Commands ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if state.monitoring:
        await update.message.reply_text("✅ Monitoring is already running.")
        return

    state.monitoring = True
    state.start_time = time.time()
    state.blocks_scanned = 0
    state.alerts_sent = 0
    state.monitor_task = asyncio.create_task(monitor_blockchain(context.application))

    await update.message.reply_text(
        f"🟢 *Ethereum monitor started!*\n"
        f"Alert threshold: *${state.threshold_usd:,.0f} USD*\n\n"
        f"Use /stop to halt monitoring.\n"
        f"Use /threshold to change the alert level.",
        parse_mode="Markdown",
    )
    logger.info("Monitoring started by user %s.", update.effective_user.id)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not state.monitoring:
        await update.message.reply_text("⚠️ Monitor is not running.")
        return

    state.monitoring = False
    if state.monitor_task:
        state.monitor_task.cancel()
        state.monitor_task = None

    await update.message.reply_text("🔴 Monitoring stopped.")
    logger.info("Monitoring stopped by user %s.", update.effective_user.id)


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            f"Current threshold: *${state.threshold_usd:,.0f} USD*\n\n"
            f"To change it, use:\n`/threshold 10000`",
            parse_mode="Markdown",
        )
        return

    try:
        new_val = float(args[0].replace(",", "").replace("$", ""))
        if new_val <= 0:
            raise ValueError("Threshold must be positive.")
        state.threshold_usd = new_val
        await update.message.reply_text(
            f"✅ Threshold updated to *${state.threshold_usd:,.0f} USD*",
            parse_mode="Markdown",
        )
        logger.info("Threshold changed to $%.2f by user %s.", new_val, update.effective_user.id)
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid value. Example: `/threshold 10000`", parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_icon = "🟢 Running" if state.monitoring else "🔴 Stopped"
    eth_price = fetch_eth_price()

    uptime_str = "—"
    if state.monitoring and state.start_time:
        elapsed = int(time.time() - state.start_time)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        uptime_str = f"{h:02d}:{m:02d}:{s:02d}"

    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"Status: {status_icon}\n"
        f"Threshold: *${state.threshold_usd:,.0f} USD*\n"
        f"ETH Price: *${eth_price:,.2f} USD*\n"
        f"Blocks Scanned: *{state.blocks_scanned:,}*\n"
        f"Alerts Sent: *{state.alerts_sent:,}*\n"
        f"Uptime: *{uptime_str}*",
        parse_mode="Markdown",
    )


# ── Entry Point ────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("Starting Ethereum Monitor Bot…")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info("Bot is polling for commands.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
