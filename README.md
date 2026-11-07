# Quotex Signal Bot 🤖

Telegram bot + REST API that generates **Quotex trade signals** using 
walk-forward validation on historical 1-minute candle data.

---

## Features

| Feature | Detail |
|---|---|
| **Telegram Bot** | Login with SSID token, add pairs, see signals |
| **REST API** | `GET /?Pair=BRLUSD_otc&Day=25` |
| **Max Analysis** | 50 days walk-forward validation |
| **Public Tunnel** | cloudflared → `trycloudflare.com` URL |
| **Railway Ready** | Dockerfile with cloudflared pre-installed |

---

## Quick Start (Railway)

1. Fork / upload this repo to GitHub
2. Create a new Railway project → **Deploy from GitHub repo**
3. Go to **Variables** and add:
   ```
   BOT_TOKEN = <your Telegram bot token from @BotFather>
   ```
4. Railway auto-detects the **Dockerfile** and builds the image
5. Done! Open your bot on Telegram

---

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/start` | Begin — bot asks for your Quotex SSID token |
| `/server` | Start cloudflared tunnel → returns public URL |
| `/status` | Show login state, loaded pairs, tunnel URL |
| `/addpair EURUSD,BRLUSD_otc` | Add more pairs |

### Login Flow

1. Open Quotex website → Press **F12** → **Console** tab
2. Type `window.settings.token` → copy the value
3. Send `/start` to the bot → paste the token
4. Bot logs in → asks for pair names
5. Send: `EURUSD,BRLUSD_otc,EURUSD_otc`
6. Bot fetches 50 days candle data
7. Send `/server` → get the public API URL

---

## API Usage

```
GET https://xyz.trycloudflare.com/?Pair=BRLUSD_otc&Day=25
```

| Parameter | Required | Description |
|---|---|---|
| `Pair` | ✅ | Asset name (e.g. `EURUSD`, `BRLUSD_otc`) |
| `Day` | ❌ | Analysis window in days (1–50, default 25) |

### Response

```json
{
  "pair": "BRLUSD_otc",
  "day": 25,
  "total": 12,
  "signals": [
    { "asset": "BRLUSD-OTC", "time": "09:15", "direction": "CALL" },
    { "asset": "BRLUSD-OTC", "time": "11:30", "direction": "PUT" }
  ]
}
```

### Health Check

```
GET https://xyz.trycloudflare.com/health
```

---

## Project Structure

```
main.py           Entry point (Flask thread + Telegram bot)
bot.py            Telegram bot handlers
api.py            Flask REST API
analysis.py       Signal engine (walk-forward validation)
shared_state.py   Thread-safe in-memory store
requirements.txt  Python dependencies
Dockerfile        Railway deployment (includes cloudflared)
pyquotex/         Quotex library (bundled directly)
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | **Required** — Telegram bot token from @BotFather |
| `PORT` | Auto-set by Railway (default 5000) |
