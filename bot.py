"""
bot.py
Telegram bot for Quotex Signal Bot.

Bot Flow
────────
  /start              → asks for Quotex SSID token
  <token>             → logs into Quotex; on success asks for pairs
  <EURUSD,BRLUSD_otc> → fetches 50-day candle data, stores in shared_state
  /server             → starts cloudflared tunnel; replies with public URL
  /addpair [pairs]    → adds more pairs after initial setup
  /status             → shows login state, loaded pairs, tunnel URL

Railway env var required:
  BOT_TOKEN           → your Telegram bot token
"""

import asyncio
import logging
import os
import re
import subprocess
import threading
from datetime import datetime
from typing import List

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import shared_state
from analysis import (
    MAX_ANALYSIS_DAYS,
    CandleFetcher,
    generate_asset_signals,
    local_now,
)

logger = logging.getLogger(__name__)
TZ     = pytz.timezone("Asia/Dhaka")

# ── Conversation state keys stored in context.user_data ──────────────────────
_STATE         = "bot_state"
IDLE           = "IDLE"
WAITING_TOKEN  = "WAITING_TOKEN"
WAITING_PAIRS  = "WAITING_PAIRS"


# ════════════════════════════════════════════════════════════════════════════
# /start  – begin login flow
# ════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[_STATE] = WAITING_TOKEN
    await update.message.reply_text(
        "🤖 *Quotex Signal Bot*\n\n"
        "🔑 *SSID Token* দিন:\n"
        "_(Quotex website → F12 → Console → `window.settings.token` copy করুন)_",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════════════════════
# /server  – start cloudflared tunnel and reply with public URL
# ════════════════════════════════════════════════════════════════════════════

async def cmd_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # If tunnel is already alive, just show the URL
    with shared_state._lock:
        url     = shared_state.tunnel_url
        process = shared_state.tunnel_process

    if url and process and process.poll() is None:
        await update.message.reply_text(
            f"✅ *Server Already Public!*\n\n"
            f"🔗 `{url}`\n\n"
            f"📡 *API Example:*\n"
            f"`{url}/?Pair=BRLUSD_otc&Day=25`\n\n"
            f"Max Day: `{MAX_ANALYSIS_DAYS}`",
            parse_mode="Markdown",
        )
        return

    # Reset stale state
    with shared_state._lock:
        shared_state.tunnel_url     = None
        shared_state.tunnel_process = None

    PORT = int(os.environ.get("PORT", 5000))
    msg  = await update.message.reply_text("⏳ Cloudflare Tunnel start হচ্ছে...")

    found_url    = []
    result_event = threading.Event()

    def _run_tunnel() -> None:
        try:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with shared_state._lock:
                shared_state.tunnel_process = proc

            for line in proc.stdout:
                if "trycloudflare.com" in line and not result_event.is_set():
                    m = re.search(r"(https://[a-zA-Z0-9-]+\\.trycloudflare\\.com)", line)
                    if m:
                        tunnel = m.group(1)
                        with shared_state._lock:
                            shared_state.tunnel_url = tunnel
                        found_url.append(tunnel)
                        result_event.set()
            # Keep consuming stdout until cloudflared exits so its pipe
            # cannot fill up and block the tunnel process.

        except FileNotFoundError:
            found_url.append("NOT_FOUND")
            result_event.set()
        except Exception as exc:
            found_url.append(f"ERROR:{exc}")
            result_event.set()

    threading.Thread(target=_run_tunnel, daemon=True).start()

    # Poll up to 30 s for the URL
    for _ in range(30):
        await asyncio.sleep(1)
        if result_event.is_set():
            break

    result = found_url[0] if found_url else None

    if result and result.startswith("https://"):
        await msg.edit_text(
            f"✅ *Server is now Public!*\n\n"
            f"🔗 `{result}`\n\n"
            f"📡 *API Example:*\n"
            f"`{result}/?Pair=BRLUSD_otc&Day=25`\n\n"
            f"ℹ️ Max Day: `{MAX_ANALYSIS_DAYS}` | Pair list: /status",
            parse_mode="Markdown",
        )
    elif result == "NOT_FOUND":
        await msg.edit_text(
            "❌ *cloudflared not found!*\n"
            "Dockerfile দিয়ে deploy করুন — cloudflared automatic install হবে।",
            parse_mode="Markdown",
        )
    else:
        await msg.edit_text(
            "❌ Tunnel URL পাওয়া যায়নি (30s timeout).\n"
            "আবার /server দিন।",
            parse_mode="Markdown",
        )


# ════════════════════════════════════════════════════════════════════════════
# /status  – show current state
# ════════════════════════════════════════════════════════════════════════════

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with shared_state._lock:
        logged      = shared_state.quotex_logged_in
        pairs       = list(shared_state.candle_data.keys())
        pair_counts = {p: len(v) for p, v in shared_state.candle_data.items()}
        url         = shared_state.tunnel_url

    login_text = "✅ Logged In" if logged else "❌ Not Logged In"
    url_text   = f"`{url}`" if url else "❌ Tunnel off — use /server"

    if pairs:
        pairs_text = "\n".join(f"  • `{p}` — {pair_counts[p]} candles" for p in pairs)
    else:
        pairs_text = "  _None yet — login করে pair দিন_"

    await update.message.reply_text(
        f"*📊 Bot Status*\n\n"
        f"*Login:* {login_text}\n"
        f"*Tunnel:* {url_text}\n\n"
        f"*Pairs Loaded:*\n{pairs_text}\n\n"
        f"More pairs add করতে: `/addpair EURUSD,BRLUSD_otc`",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════════════════════
# /addpair  – add more pairs after initial setup
# ════════════════════════════════════════════════════════════════════════════

async def cmd_addpair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with shared_state._lock:
        logged = shared_state.quotex_logged_in
        ssid   = shared_state.ssid_token

    if not logged or not ssid:
        await update.message.reply_text(
            "❌ Login করা নেই। আগে /start দিয়ে login করুন।"
        )
        return

    # Accept pairs from args: /addpair EURUSD,BRLUSD_otc
    raw = " ".join(context.args).strip() if context.args else ""
    if raw:
        pairs = [p.strip() for p in raw.split(",") if p.strip()]
        if pairs:
            await _fetch_pairs(update, context, pairs, ssid)
            return

    # No args → switch to WAITING_PAIRS state
    context.user_data[_STATE] = WAITING_PAIRS
    await update.message.reply_text(
        "📊 Pair নাম দিন (comma দিয়ে):\n"
        "_Example: EURUSD,BRLUSD_otc,EURUSD_otc_",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════════════════════
# General text message router
# ════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get(_STATE, IDLE)
    text  = update.message.text.strip()

    if state == WAITING_TOKEN:
        await _do_login(update, context, text)

    elif state == WAITING_PAIRS:
        pairs = [p.strip() for p in text.split(",") if p.strip()]
        if not pairs:
            await update.message.reply_text(
                "❌ Invalid input.\n"
                "Example: `EURUSD,BRLUSD_otc`",
                parse_mode="Markdown",
            )
            return
        with shared_state._lock:
            ssid = shared_state.ssid_token
        if not ssid:
            await update.message.reply_text(
                "❌ Session শেষ। /start দিয়ে আবার login করুন।"
            )
            context.user_data[_STATE] = IDLE
            return
        await _fetch_pairs(update, context, pairs, ssid)

    else:
        await update.message.reply_text(
            "Commands:\n"
            "/start — login করুন\n"
            "/addpair — নতুন pair যোগ করুন\n"
            "/server — Public URL পান\n"
            "/status — current status দেখুন"
        )


# ════════════════════════════════════════════════════════════════════════════
# Internal: Quotex login
# ════════════════════════════════════════════════════════════════════════════

async def _do_login(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
) -> None:
    if not token:
        await update.message.reply_text("❌ Empty token. SSID token দিন।")
        return

    msg = await update.message.reply_text("⏳ Quotex-এ login হচ্ছে...")

    try:
        from pyquotex.stable_api import Quotex
        client = Quotex(lang="en", host="quotex.io")
        client.set_token_csrf(ssid_token=token)
        success, reason = await client.connect()
        await client.close()
    except Exception as exc:
        await msg.edit_text(f"❌ Login error: `{exc}`", parse_mode="Markdown")
        return

    if success:
        with shared_state._lock:
            shared_state.ssid_token       = token
            shared_state.quotex_logged_in = True
        context.user_data[_STATE] = WAITING_PAIRS
        await msg.edit_text(
            "✅ *Login Successful!*\n\n"
            "📊 এখন Pair নাম দিন (comma দিয়ে):\n"
            "_Example: EURUSD,BRLUSD_otc,EURUSD_otc_",
            parse_mode="Markdown",
        )
    else:
        await msg.edit_text(
            f"❌ *Login Failed*\n"
            f"Reason: `{reason}`\n\n"
            f"Token চেক করে /start দিয়ে আবার try করুন।",
            parse_mode="Markdown",
        )


# ════════════════════════════════════════════════════════════════════════════
# Internal: fetch candle data for a list of pairs
# ════════════════════════════════════════════════════════════════════════════

async def _fetch_pairs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pairs: List[str],
    ssid: str,
) -> None:
    status_msg = await update.message.reply_text(
        f"⏳ {len(pairs)}টি pair-এর data fetch হচ্ছে...\n"
        f"({MAX_ANALYSIS_DAYS} দিনের candle data)"
    )

    fetcher = CandleFetcher(ssid=ssid)
    lines: List[str] = []

    try:
        connected = await fetcher.connect()
        if not connected:
            await status_msg.edit_text(
                "❌ Quotex connection failed!\n"
                "Token expire হয়েছে? /start দিয়ে আবার login করুন।"
            )
            return

        for i, pair in enumerate(pairs, 1):
            await status_msg.edit_text(
                f"⏳ Fetching `{pair}`... ({i}/{len(pairs)})",
                parse_mode="Markdown",
            )

            candles = await fetcher.fetch(pair, MAX_ANALYSIS_DAYS)

            # Store in shared state (Flask API reads from here)
            with shared_state._lock:
                shared_state.candle_data[pair] = candles

            if candles:
                today   = local_now().date()
                signals = generate_asset_signals(pair, candles, MAX_ANALYSIS_DAYS, today)
                lines.append(
                    f"✅ `{pair}` — {len(candles)} candles | "
                    f"{len(signals)} signals ({MAX_ANALYSIS_DAYS}d)"
                )
            else:
                lines.append(f"⚠️ `{pair}` — data পাওয়া যায়নি")

    finally:
        await fetcher.close()

    context.user_data[_STATE] = IDLE
    summary = "\n".join(lines)

    with shared_state._lock:
        url = shared_state.tunnel_url

    api_note = (
        f"📡 API URL: `{url}`\n"
        f"Example: `{url}/?Pair={pairs[0]}&Day=25`"
        if url else
        "📡 /server command দিয়ে Public URL পান"
    )

    await status_msg.edit_text(
        f"*✅ Done!*\n\n{summary}\n\n{api_note}",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════════════════════
# Application builder  (called from main.py)
# ════════════════════════════════════════════════════════════════════════════

def run_bot() -> None:
    """Build and run the Telegram bot using PTB's synchronous lifecycle."""
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set!\n"
            "Railway Variables section-এ BOT_TOKEN add করুন।"
        )

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start",   cmd_start))
    application.add_handler(CommandHandler("server",  cmd_server))
    application.add_handler(CommandHandler("status",  cmd_status))
    application.add_handler(CommandHandler("addpair", cmd_addpair))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Telegram bot polling started.")
    application.run_polling(
        drop_pending_updates=True,
        close_loop=False,
    )
