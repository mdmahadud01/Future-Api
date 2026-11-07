"""
api.py
Flask REST API — serves Quotex trade signals.

Endpoints:
  GET /?Pair=BRLUSD_otc&Day=25
      Returns JSON signals for the given pair using the last N days.
      Day is capped at MAX_ANALYSIS_DAYS (50).

  GET /health
      Returns login status, loaded pairs, and tunnel URL.

The Flask app is started in a background thread by main.py.
Candle data is read from shared_state (written by the Telegram bot).
"""

import os
from datetime import datetime

import pytz
from flask import Flask, jsonify, request

import shared_state
from analysis import MAX_ANALYSIS_DAYS, generate_asset_signals

app = Flask(__name__)
TZ  = pytz.timezone("Asia/Dhaka")


# ── Signals endpoint ──────────────────────────────────────────────────────────

@app.route("/")
def signals():
    """
    Query: ?Pair=BRLUSD_otc&Day=25
    Returns:
    {
      "pair":    "BRLUSD_otc",
      "day":     25,
      "total":   12,
      "signals": [
        { "asset": "BRLUSD-OTC", "time": "09:15", "direction": "CALL" },
        ...
      ]
    }
    """
    pair    = request.args.get("Pair", "").strip()
    day_str = request.args.get("Day", "25")

    # Validate Pair
    if not pair:
        return jsonify({
            "error":   "Missing required parameter: Pair",
            "example": "/?Pair=BRLUSD_otc&Day=25",
            "signals": [],
        }), 400

    # Validate Day
    try:
        day = int(day_str)
        day = max(1, min(day, MAX_ANALYSIS_DAYS))
    except ValueError:
        return jsonify({
            "error":   f"Invalid Day value: {day_str!r} (must be 1–{MAX_ANALYSIS_DAYS})",
            "signals": [],
        }), 400

    # Look up candle data (try original case, upper, lower)
    with shared_state._lock:
        candles = (
            shared_state.candle_data.get(pair)
            or shared_state.candle_data.get(pair.upper())
            or shared_state.candle_data.get(pair.lower())
        )

    if candles is None:
        available = list(shared_state.candle_data.keys())
        return jsonify({
            "error":     f"No candle data found for pair '{pair}'.",
            "hint":      "Add this pair via the Telegram bot first.",
            "available": available,
            "signals":   [],
        }), 404

    # Generate signals on-demand for the requested day window
    today   = datetime.now(TZ).date()
    result  = generate_asset_signals(pair, candles, day, today)

    return jsonify({
        "pair":    pair,
        "day":     day,
        "total":   len(result),
        "signals": result,
    })


# ── Health endpoint ───────────────────────────────────────────────────────────

@app.route("/health")
def health():
    with shared_state._lock:
        logged      = shared_state.quotex_logged_in
        pairs       = list(shared_state.candle_data.keys())
        pair_counts = {p: len(v) for p, v in shared_state.candle_data.items()}
        turl        = shared_state.tunnel_url

    return jsonify({
        "status":        "ok",
        "logged_in":     logged,
        "pairs_loaded":  pairs,
        "candle_counts": pair_counts,
        "tunnel_url":    turl,
        "max_day":       MAX_ANALYSIS_DAYS,
    })
