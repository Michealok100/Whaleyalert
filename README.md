# 🔍 Ethereum Transaction Monitor — Telegram Bot

A production-ready Python bot that watches the Ethereum mainnet in real time and sends Telegram alerts whenever a transaction exceeds a configurable USD threshold.

---

## Features

| Feature | Detail |
|---|---|
| Real-time monitoring | Subscribes to new block headers via WebSocket |
| USD conversion | Fetches live ETH/USD from CoinGecko (cached 30 s) |
| Configurable threshold | Default $5,000 — change any time with `/threshold` |
| Auto-reconnect | Exponential back-off up to 2 min if the WS drops |
| Telegram commands | `/start` `/stop` `/threshold` `/status` |
| Structured logging | Console + rotating file (`eth_monitor.log`) |

---

## Project Structure

```
project/
├── bot.py           # Main bot + blockchain monitor logic
├── config.py        # All configuration (API keys, thresholds, …)
├── requirements.txt # Python dependencies
└── README.md        # This file
```

---

## Step-by-Step Setup

### 1 — Python environment

```bash
# Requires Python 3.10+
python3 --version

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # macOS / Linux
venv\Scripts\activate             # Windows

# Install dependencies
pip install -r requirements.txt
```

---

### 2 — Create a Telegram Bot with BotFather

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (choose a name and username).
3. BotFather will reply with your **Bot Token** — copy it.
4. To get your **Chat ID**:
   - Start a conversation with your new bot (send `/start`).
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
   - Find `"chat":{"id": 123456789}` — that number is your Chat ID.
   - For a group, add the bot to the group and repeat the step above; the ID will be negative (e.g. `-100123456789`).

---

### 3 — Get an Ethereum WebSocket Endpoint

#### Option A — Infura (recommended for beginners)

1. Sign up at <https://app.infura.io/>.
2. Create a new project → select **Ethereum** → **Mainnet**.
3. Copy the **WebSocket** URL:
   ```
   wss://mainnet.infura.io/ws/v3/YOUR_PROJECT_ID
   ```

#### Option B — Alchemy

1. Sign up at <https://dashboard.alchemy.com/>.
2. Create a new app → **Ethereum** → **Mainnet**.
3. Click **View Key** → copy the **WebSocket** URL:
   ```
   wss://eth-mainnet.g.alchemy.com/v2/YOUR_API_KEY
   ```

---

### 4 — Configure the Bot

Edit **`config.py`** and replace the placeholder values:

```python
TELEGRAM_BOT_TOKEN = "123456:ABCdef..."        # from BotFather
TELEGRAM_CHAT_ID   = "123456789"               # your Chat ID
WEB3_WSS_ENDPOINT  = "wss://mainnet.infura.io/ws/v3/YOUR_PROJECT_ID"
```

Or set environment variables (they override `config.py`):

```bash
export TELEGRAM_BOT_TOKEN="123456:ABCdef..."
export TELEGRAM_CHAT_ID="123456789"
export WEB3_WSS_ENDPOINT="wss://mainnet.infura.io/ws/v3/YOUR_PROJECT_ID"
export DEFAULT_THRESHOLD_USD="5000"
```

---

### 5 — Run the Bot

```bash
python bot.py
```

You should see:

```
2025-01-01 12:00:00 [INFO] eth_monitor: Starting Ethereum Monitor Bot…
2025-01-01 12:00:00 [INFO] eth_monitor: Bot is polling for commands.
```

Now open Telegram and send `/start` to your bot.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Begin monitoring the blockchain |
| `/stop` | Pause monitoring |
| `/threshold 10000` | Set alert threshold to $10,000 USD |
| `/threshold` | View current threshold |
| `/status` | Show monitoring state, ETH price, stats |

---

## Example Alert

```
🚨 Large Ethereum Transaction Detected

💰 Value: $7,230.45  |  Amount: 2.401234 ETH
📤 From: 0x123...abc
📥 To:   0x456...def
🔗 Tx Hash: 0x987...xyz
🔍 View on Etherscan
```

---

## Running as a Service (Linux / systemd)

Create `/etc/systemd/system/eth-monitor.service`:

```ini
[Unit]
Description=Ethereum Transaction Monitor Bot
After=network.target

[Service]
WorkingDirectory=/path/to/project
ExecStart=/path/to/project/venv/bin/python bot.py
Restart=on-failure
RestartSec=10
EnvironmentFile=/path/to/project/.env   # optional env file

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable eth-monitor
sudo systemctl start eth-monitor
sudo journalctl -u eth-monitor -f    # follow logs
```

---

## Running with Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t eth-monitor .
docker run -d \
  -e TELEGRAM_BOT_TOKEN="..." \
  -e TELEGRAM_CHAT_ID="..." \
  -e WEB3_WSS_ENDPOINT="..." \
  --name eth-monitor \
  eth-monitor
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ConnectionError: Web3 provider not connected` | Check your WSS URL and that you're not behind a restrictive firewall |
| `Unauthorized` from Telegram | Verify `TELEGRAM_BOT_TOKEN` is correct |
| No alerts despite large txs | Confirm the bot was started with `/start` and check the threshold with `/status` |
| CoinGecko 429 errors | The 30 s price cache should prevent this; increase `ETH_PRICE_CACHE_SECONDS` if needed |

---

## Security Notes

- Never commit `config.py` with real API keys to a public repo. Add it to `.gitignore` or use environment variables.
- Restrict Telegram bot access by checking `update.effective_user.id` if you share the bot token.

---

## License

MIT
