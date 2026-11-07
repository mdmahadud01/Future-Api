"""
shared_state.py
Thread-safe in-memory store shared between:
  - The Telegram bot (async, main thread event loop)
  - The Flask API  (sync, background thread)
"""

import threading
from typing import Optional

# ── Lock ──────────────────────────────────────────────────────────────────────
# Use RLock so the same thread can acquire it multiple times without deadlocking.
_lock = threading.RLock()

# ── Quotex session ────────────────────────────────────────────────────────────
ssid_token: Optional[str]  = None
quotex_logged_in: bool     = False

# ── Candle data ───────────────────────────────────────────────────────────────
# Key   : pair name as given by the user (e.g. "BRLUSD_otc")
# Value : list of normalised candle dicts from analysis.normalize_candle()
candle_data: dict = {}

# ── Cloudflare tunnel ─────────────────────────────────────────────────────────
tunnel_url: Optional[str] = None
tunnel_process            = None   # subprocess.Popen instance
