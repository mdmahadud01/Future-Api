"""
analysis.py
Signal analysis engine.

- Fetches historical 1-minute candles from Quotex via pyquotex
- Applies walk-forward validation to find statistically reliable minutes
- Returns trade signals: { asset, time, direction }

Max analysis window: MAX_ANALYSIS_DAYS = 50
"""

import asyncio
import logging
import os
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytz
from pyquotex.stable_api import Quotex

# ── Configuration ─────────────────────────────────────────────────────────────
TZ     = pytz.timezone("Asia/Dhaka")
PERIOD = 60                   # 1-minute candles (seconds)

MAX_ANALYSIS_DAYS = 50        # Hard cap – API will never exceed this

# Walk-forward filter thresholds (same as original API.py)
MIN_TRAIN_SAMPLES       = 10
MIN_TRAIN_AGREEMENT     = 0.75
MIN_VALIDATION_SAMPLES  = 8
MIN_VALIDATION_ACCURACY = 0.70
MIN_TRAIN_DAYS          = 8

SUPPRESS_NEARBY_SIGNALS = True
SIGNAL_GAP_MINUTES      = 3
MAX_SIGNALS_PER_ASSET   = None   # None = unlimited

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"


# ── Time helpers ──────────────────────────────────────────────────────────────

def local_now() -> datetime:
    """Current local datetime in Asia/Dhaka timezone."""
    return datetime.now(TZ)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Convert a unix timestamp (ms or s) or ISO string to a timezone-aware datetime."""
    if value is None:
        return None
    try:
        ts = float(value)
        if ts > 1_000_000_000_000:          # milliseconds → seconds
            ts /= 1000
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(TZ)
        except (ValueError, TypeError, OverflowError):
            return None


def normalize_candle(raw: dict) -> Optional[dict]:
    """Validate and normalise a raw candle dict from pyquotex."""
    try:
        raw_time = raw.get("time") or raw.get("timestamp")
        dt = parse_timestamp(raw_time)
        if dt is None:
            return None

        o = float(raw.get("open",  raw.get("o", 0)))
        c = float(raw.get("close", raw.get("c", 0)))
        h = float(raw.get("high",  raw.get("h", 0)))
        l = float(raw.get("low",   raw.get("l", 0)))

        # Reject NaN / infinity
        if not all(map(lambda x: x == x and abs(x) != float("inf"), (o, c, h, l))):
            return None
        # Sanity: high ≥ open,close and low ≤ open,close
        if h < max(o, c) or l > min(o, c) or h < l:
            return None

        return {
            "dt":     dt,
            "date":   dt.date(),
            "minute": dt.hour * 60 + dt.minute,   # 0 – 1439
            "open":   o,
            "close":  c,
        }
    except (ValueError, TypeError, KeyError):
        return None


def direction_of(candle: dict) -> Optional[str]:
    """Return 'CALL', 'PUT', or None for a doji."""
    if candle["close"] > candle["open"]:
        return "CALL"
    if candle["close"] < candle["open"]:
        return "PUT"
    return None


# ── Quotex data fetcher ───────────────────────────────────────────────────────

class CandleFetcher:
    """
    Async helper that opens a pyquotex session and fetches historical candles.

    Usage:
        fetcher = CandleFetcher(ssid="…")
        await fetcher.connect()
        candles = await fetcher.fetch("BRLUSD_otc", days=50)
        await fetcher.close()
    """

    def __init__(self, ssid: str, csrf: Optional[str] = None):
        self.ssid   = ssid
        self.csrf   = csrf
        self.client: Optional[Quotex] = None
        self.connected = False

    async def connect(self) -> bool:
        try:
            self.client = Quotex(lang="en", host="quotex.io")
            self.client.set_token_csrf(ssid_token=self.ssid, csrf_token=self.csrf)
            success, _ = await self.client.connect()
            if not success:
                return False
            self.client.set_account_mode("PRACTICE")
            self.connected = True
            return True
        except Exception:
            return False

    async def fetch(self, asset: str, days: int) -> List[dict]:
        """Fetch up to *days* days of 1-minute candles for *asset*."""
        days = min(days, MAX_ANALYSIS_DAYS)
        if not self.connected:
            return []
        try:
            raw = await self.client.get_historical_candles(
                asset=asset,
                amount_of_seconds=(days + 3) * 1440 * PERIOD,
                period=PERIOD,
                timeout=30,
                max_workers=5,
            )
            if not raw:
                return []

            # Deduplicate by rounded timestamp, then sort chronologically
            unique: Dict[float, dict] = {}
            for item in raw:
                candle = normalize_candle(item)
                if candle:
                    key = candle["dt"].replace(second=0, microsecond=0).timestamp()
                    unique[key] = candle
            return [unique[k] for k in sorted(unique)]
        except Exception:
            return []

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.connected = False


# ── Historical data grouping ──────────────────────────────────────────────────

def group_by_day(candles: List[dict], first_day, last_day) -> dict:
    """
    Group candles into { date: { minute: direction } } for the window
    [first_day, last_day].
    """
    daily: Dict = defaultdict(dict)
    for candle in candles:
        day = candle["date"]
        if not (first_day <= day <= last_day):
            continue
        direction = direction_of(candle)
        if direction is None:
            continue
        daily[day][candle["minute"]] = direction
    return daily


def majority_direction(directions: List[str]):
    """
    Return (dominant_direction, agreement_ratio) for a list of 'CALL'/'PUT'.
    Returns (None, 0.0) on tie or empty list.
    """
    calls = directions.count("CALL")
    puts  = directions.count("PUT")
    total = calls + puts
    if total == 0 or calls == puts:
        return None, 0.0
    if calls > puts:
        return "CALL", calls / total
    return "PUT", puts / total


# ── Walk-forward validation ───────────────────────────────────────────────────

def evaluate_minute(day_data: dict, minute: int, ordered_days: list) -> dict:
    """
    Walk-forward test for a single minute across all available days.

    For each day d (after MIN_TRAIN_DAYS training days):
      - Use all prior days as training set
      - Predict the direction with majority vote
      - Score against the actual direction on day d

    Returns a stats dict (direction, agreement, samples, validation_samples,
    validation_accuracy).
    """
    actual_results = []

    for index in range(MIN_TRAIN_DAYS, len(ordered_days)):
        train_days = ordered_days[:index]
        test_day   = ordered_days[index]

        training = [day_data[d][minute] for d in train_days if minute in day_data[d]]
        actual   = day_data[test_day].get(minute)

        if len(training) < MIN_TRAIN_SAMPLES:
            continue
        if len(set(train_days)) < MIN_TRAIN_DAYS:
            continue

        prediction, agreement = majority_direction(training)
        if prediction is None or actual is None:
            continue
        if agreement < MIN_TRAIN_AGREEMENT:
            continue

        actual_results.append(prediction == actual)

    validation_n        = len(actual_results)
    validation_accuracy = sum(actual_results) / validation_n if validation_n else 0.0

    # Final direction from ALL available days
    final_history                = [day_data[d][minute] for d in ordered_days if minute in day_data[d]]
    final_direction, final_agreement = majority_direction(final_history)

    return {
        "direction":           final_direction,
        "agreement":           final_agreement,
        "samples":             len(final_history),
        "validation_samples":  validation_n,
        "validation_accuracy": validation_accuracy,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def generate_asset_signals(
    asset: str,
    candles: List[dict],
    analysis_days: int,
    today,
) -> List[dict]:
    """
    Generate trade signals for *asset* using the last *analysis_days* days
    of *candles*.  analysis_days is capped at MAX_ANALYSIS_DAYS (50).

    Returns:
        List of dicts: { "asset": str, "time": "HH:MM", "direction": "CALL"|"PUT" }
    """
    analysis_days = min(analysis_days, MAX_ANALYSIS_DAYS)

    first_day    = today - timedelta(days=analysis_days)
    last_day     = today - timedelta(days=1)

    day_data     = group_by_day(candles, first_day, last_day)
    ordered_days = sorted(day_data.keys())

    if len(ordered_days) < (MIN_TRAIN_DAYS + MIN_VALIDATION_SAMPLES):
        return []

    candidates = []

    for minute in range(1440):
        stats = evaluate_minute(day_data, minute, ordered_days)

        if stats["direction"] is None:
            continue
        if stats["samples"]             < MIN_TRAIN_SAMPLES:
            continue
        if stats["agreement"]           < MIN_TRAIN_AGREEMENT:
            continue
        if stats["validation_samples"]  < MIN_VALIDATION_SAMPLES:
            continue
        if stats["validation_accuracy"] < MIN_VALIDATION_ACCURACY:
            continue

        h, m = divmod(minute, 60)
        candidates.append({
            "asset":       asset.replace("_otc", "-OTC"),
            "time":        f"{h:02d}:{m:02d}",
            "direction":   stats["direction"],
            "_accuracy":   stats["validation_accuracy"],
            "_agreement":  stats["agreement"],
            "_samples":    stats["samples"],
            "_validation": stats["validation_samples"],
        })

    # Sort best signals first
    candidates.sort(
        key=lambda x: (x["_accuracy"], x["_agreement"], x["_validation"], x["_samples"]),
        reverse=True,
    )

    # Apply nearby-signal suppression
    selected: List[dict] = []
    for candidate in candidates:
        cm = int(candidate["time"][:2]) * 60 + int(candidate["time"][3:])
        if SUPPRESS_NEARBY_SIGNALS:
            too_close = any(
                abs(cm - (int(e["time"][:2]) * 60 + int(e["time"][3:]))) <= SIGNAL_GAP_MINUTES
                for e in selected
            )
            if too_close:
                continue
        selected.append(candidate)
        if MAX_SIGNALS_PER_ASSET and len(selected) >= MAX_SIGNALS_PER_ASSET:
            break

    # Strip internal metadata before returning
    return [
        {"asset": s["asset"], "time": s["time"], "direction": s["direction"]}
        for s in selected
    ]
