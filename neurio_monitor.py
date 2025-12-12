#!/usr/bin/env python3
"""
Neurio power monitor + smart analytics + enhanced appliance detection
Single-file version.

Features:
- Poll Neurio every second
- Aggregate into 2-minute energy blocks with TOU cost
- Enhanced appliance detection (multi-feature matching)
- Appliance event logging with cost
- Monthly bill prediction, goal tracking
- Appliance cost breakdown
- Peak demand warning
- Vacation mode pattern check
- Carbon footprint & tomorrow forecast
- Anomaly detection for high baseline
- Insights table (TOU savings, anomalies)
- Simple JSON API for frontend dashboard
"""

import os
import time
import threading
import json
import sqlite3
from collections import deque, defaultdict
from datetime import datetime, timedelta, date
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict, Tuple

import requests
from flask import Flask, jsonify, request

# ----------------- Config -----------------

NEURIO_URL = os.getenv("NEURIO_URL", "http://192.168.50.165/current-sample")
DB_PATH = os.getenv("NEURIO_DB_PATH", "/home/rarruda/neurio_power.db")

POLL_INTERVAL_SECONDS = 1.0        # poll Neurio every 1 second
AGGREGATE_INTERVAL_SECONDS = 120   # aggregate every 2 minutes
LIVE_WINDOW_SECONDS = 15 * 60      # keep 15 minutes of live data
RETENTION_DAYS = 365               # auto prune > 12 months

# TOU rates in dollars per kWh
DEFAULT_RATES = {
    "winter": {
        "off_peak": 0.098,
        "mid_peak": 0.157,
        "on_peak": 0.203,
    },
    "summer": {
        "off_peak": 0.098,
        "mid_peak": 0.157,
        "on_peak": 0.203,
    },
}

DAILY_BUDGET_DEFAULT = 5.00  # not heavily used here, but kept for future

app = Flask(__name__)

# ----------------- In-memory state -----------------

recent_samples: deque[Tuple[float, Optional[float]]] = deque(
    maxlen=LIVE_WINDOW_SECONDS
)  # (ts, power_w)

agg_buffer: List[Tuple[float, Optional[float]]] = []
last_agg_ts: Optional[float] = None

# TOU rates cache
RATES: Dict[str, Dict[str, float]] = DEFAULT_RATES.copy()

# Settings cache (very light usage here)
DAILY_BUDGET = DAILY_BUDGET_DEFAULT

# Enhanced detection engine
enhanced_detector = None  # ApplianceDetector instance (set in main)

# Anomaly / discovery state
anomaly_state = {
    "recent_events": deque(maxlen=100),
    "last_check": 0.0,
    "alerts": [],
}

discovery_state = {
    "active": False,
    "start_ts": None,
    "discovered": [],
}

state_lock = threading.Lock()


# ----------------- DB helpers -----------------

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # Aggregated samples
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            ts REAL PRIMARY KEY,
            power_w REAL NOT NULL,
            energy_kwh REAL NOT NULL,
            cost REAL NOT NULL,
            season TEXT NOT NULL,
            tou_period TEXT NOT NULL,
            rate REAL NOT NULL
        )
        """
    )

    # Enhanced appliance signatures (v2)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS appliance_signatures_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            baseline_w REAL NOT NULL,
            startup_w REAL NOT NULL,
            running_w REAL NOT NULL,
            shutdown_w REAL NOT NULL,
            std_dev_w REAL NOT NULL,
            min_duration_s INTEGER NOT NULL,
            max_duration_s INTEGER NOT NULL,
            has_startup_spike INTEGER NOT NULL,
            has_cycling INTEGER NOT NULL,
            cycle_period_s INTEGER,
            created_ts INTEGER NOT NULL,
            sample_count INTEGER NOT NULL
        )
        """
    )

    # Detected appliance events
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS appliance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appliance_name TEXT NOT NULL,
            start_ts REAL NOT NULL,
            end_ts REAL NOT NULL,
            avg_w REAL NOT NULL,
            energy_kwh REAL NOT NULL,
            cost REAL NOT NULL,
            confidence REAL NOT NULL
        )
        """
    )

    # Simple settings store
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Anomaly alerts
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS anomaly_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            type TEXT,
            message TEXT,
            severity TEXT,
            acknowledged INTEGER DEFAULT 0,
            created_ts REAL
        )
        """
    )

    # Smart insights (e.g. TOU savings suggestions)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            type TEXT,
            message TEXT,
            potential_savings REAL,
            dismissed INTEGER DEFAULT 0,
            created_ts REAL
        )
        """
    )

    conn.commit()
    conn.close()


def prune_old_data():
    cutoff = time.time() - RETENTION_DAYS * 24 * 3600
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM appliance_events WHERE start_ts < ?", (cutoff,))
        conn.execute("DELETE FROM anomaly_alerts WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM insights WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        if row is None:
            return default
        return row["value"]
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------- TOU helpers -----------------

def classify_tou(dt_local: datetime) -> Tuple[str, str]:
    """Return (season, tou_period) for Ontario-style TOU."""
    month = dt_local.month
    weekday = dt_local.weekday()
    hour = dt_local.hour

    if month in (11, 12, 1, 2, 3):
        season = "winter"
    else:
        season = "summer"

    is_weekend = weekday >= 5

    if is_weekend:
        return season, "off_peak"

    if season == "winter":
        if 7 <= hour < 11 or 17 <= hour < 19:
            return season, "on_peak"
        elif 11 <= hour < 17:
            return season, "mid_peak"
        else:
            return season, "off_peak"
    else:
        # summer
        if 11 <= hour < 17:
            return season, "on_peak"
        elif 7 <= hour < 11 or 17 <= hour < 19:
            return season, "mid_peak"
        else:
            return season, "off_peak"


def get_tou_info(ts: float) -> Tuple[str, str, float]:
    """Return (season, tou_period, rate_dollars_per_kwh)."""
    dt = datetime.fromtimestamp(ts)
    season, period = classify_tou(dt)
    rate = RATES.get(season, {}).get(period, 0.10)
    return season, period, rate


# ----------------- Enhanced appliance detection -----------------

class ApplianceState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass
class ApplianceSignature:
    id: int
    name: str

    baseline_w: float
    startup_w: float
    running_w: float
    shutdown_w: float

    std_dev_w: float
    min_duration_s: int
    max_duration_s: int

    has_startup_spike: bool
    has_cycling: bool
    cycle_period_s: Optional[int]

    created_ts: int
    sample_count: int


@dataclass
class ApplianceEventState:
    signature: ApplianceSignature
    state: ApplianceState
    start_ts: float
    last_update_ts: float
    samples: List[float]          # delta power samples
    peak_w: float
    avg_w: float
    confidence_scores: List[float]

    def duration_s(self) -> float:
        return self.last_update_ts - self.start_ts

    def avg_confidence(self) -> float:
        if not self.confidence_scores:
            return 0.0
        return sum(self.confidence_scores) / len(self.confidence_scores)


class FeatureExtractor:
    """Feature extraction helpers."""

    @staticmethod
    def baseline_from_samples(samples: List[Tuple[float, float]], percentile: float = 10) -> float:
        powers = [p for _, p in samples if p is not None]
        if not powers:
            return 0.0
        powers_sorted = sorted(powers)
        idx = int(len(powers_sorted) * percentile / 100.0)
        idx = max(0, min(idx, len(powers_sorted) - 1))
        return float(powers_sorted[idx])

    @staticmethod
    def analyze_power_profile(samples: List[Tuple[float, float]], baseline: float) -> Dict:
        powers = [max(0.0, p - baseline) for _, p in samples if p is not None]
        if not powers:
            return {
                "mean": 0.0,
                "std": 0.0,
                "min": 0.0,
                "max": 0.0,
                "startup_spike": 0.0,
                "has_cycling": False,
                "cycle_period": None,
            }

        n = len(powers)
        mean_w = sum(powers) / n
        var = sum((x - mean_w) ** 2 for x in powers) / n
        std_w = var ** 0.5
        min_w = min(powers)
        max_w = max(powers)

        # Startup spike: first 10% vs rest
        split = max(1, n // 10)
        startup = powers[:split]
        steady = powers[split:] or powers
        startup_mean = sum(startup) / len(startup)
        steady_mean = sum(steady) / len(steady)
        startup_spike = startup_mean - steady_mean if startup_mean > steady_mean * 1.3 else 0.0

        # Very simple cycling heuristic (variance)
        has_cycling = std_w > max(10.0, mean_w * 0.1)
        cycle_period = 60 if has_cycling else None

        return {
            "mean": mean_w,
            "std": std_w,
            "min": min_w,
            "max": max_w,
            "startup_spike": startup_spike,
            "has_cycling": has_cycling,
            "cycle_period": cycle_period,
        }


class ApplianceDetector:
    """Multi-feature appliance detector."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.signatures: List[ApplianceSignature] = []
        self.active_events: Dict[int, ApplianceEventState] = {}
        self.feature_extractor = FeatureExtractor()

        self.min_power_threshold = 30.0
        self.startup_window_samples = 5
        self.confidence_threshold = 0.6
        self.debounce_seconds = 3.0

        self.recent_samples: deque[Tuple[float, float]] = deque(maxlen=900)
        self.baseline_cache = 0.0
        self.baseline_counter = 0

        self.load_signatures()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def load_signatures(self):
        self.signatures.clear()
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM appliance_signatures_v2 ORDER BY created_ts DESC")
            for row in cur:
                self.signatures.append(
                    ApplianceSignature(
                        id=row["id"],
                        name=row["name"],
                        baseline_w=row["baseline_w"],
                        startup_w=row["startup_w"],
                        running_w=row["running_w"],
                        shutdown_w=row["shutdown_w"],
                        std_dev_w=row["std_dev_w"],
                        min_duration_s=row["min_duration_s"],
                        max_duration_s=row["max_duration_s"],
                        has_startup_spike=bool(row["has_startup_spike"]),
                        has_cycling=bool(row["has_cycling"]),
                        cycle_period_s=row["cycle_period_s"],
                        created_ts=row["created_ts"],
                        sample_count=row["sample_count"],
                    )
                )
        finally:
            conn.close()

    def update_baseline(self):
        self.baseline_counter += 1
        if self.baseline_counter < 60:
            return
        self.baseline_counter = 0
        if len(self.recent_samples) < 10:
            return
        self.baseline_cache = self.feature_extractor.baseline_from_samples(
            list(self.recent_samples), percentile=15
        )

    def compute_match_score(
        self,
        current_w: float,
        baseline: float,
        signature: ApplianceSignature,
        recent_deltas: List[float],
    ) -> float:
        delta = current_w - baseline
        if delta < self.min_power_threshold:
            return 0.0

        scores = []
        # Power level (40%)
        expected = signature.running_w
        diff = abs(delta - expected)
        threshold = max(expected * 0.35, 100.0)
        if diff > threshold:
            return 0.0
        power_score = 1.0 - diff / threshold
        scores.append((power_score, 0.4))

        # Variance (20%)
        if len(recent_deltas) >= 5:
            mean = sum(recent_deltas) / len(recent_deltas)
            var = sum((x - mean) ** 2 for x in recent_deltas) / len(recent_deltas)
            std = var ** 0.5
            if signature.std_dev_w > 0:
                ratio = min(std, signature.std_dev_w) / max(std, signature.std_dev_w)
                scores.append((ratio, 0.2))

        # Startup spike (20%)
        if signature.has_startup_spike and len(recent_deltas) >= self.startup_window_samples:
            recent_max = max(recent_deltas[-self.startup_window_samples:])
            if recent_max > signature.startup_w * 0.8:
                scores.append((1.0, 0.2))
            else:
                scores.append((0.5, 0.2))
        else:
            scores.append((1.0, 0.2))

        # Duration feasibility (20%) – we accept, refine on stop
        scores.append((1.0, 0.2))

        return sum(s * w for s, w in scores)

    def process_sample(self, ts: float, power_w: Optional[float]) -> List[Dict]:
        if power_w is None:
            return []

        self.recent_samples.append((ts, power_w))
        self.update_baseline()

        baseline = self.baseline_cache
        delta = power_w - baseline
        recent_deltas = [p - baseline for _, p in list(self.recent_samples)[-10:] if p is not None]

        best_id = None
        best_score = 0.0
        best_sig = None

        for sig in self.signatures:
            score = self.compute_match_score(power_w, baseline, sig, recent_deltas)
            if score > best_score and score >= self.confidence_threshold:
                best_id = sig.id
                best_score = score
                best_sig = sig

        events: List[Dict] = []

        if best_id is not None and best_sig is not None:
            event = self.active_events.get(best_id)
            if event is None:
                # start new
                event = ApplianceEventState(
                    signature=best_sig,
                    state=ApplianceState.STARTING,
                    start_ts=ts,
                    last_update_ts=ts,
                    samples=[delta],
                    peak_w=delta,
                    avg_w=delta,
                    confidence_scores=[best_score],
                )
                self.active_events[best_id] = event
                events.append(
                    {
                        "type": "started",
                        "signature_id": best_id,
                        "appliance": best_sig.name,
                        "start_ts": ts,
                        "initial_w": delta,
                        "confidence": best_score,
                    }
                )
            else:
                event.last_update_ts = ts
                event.samples.append(delta)
                event.peak_w = max(event.peak_w, delta)
                event.avg_w = sum(event.samples) / len(event.samples)
                event.confidence_scores.append(best_score)

                # trim
                if len(event.samples) > 100:
                    event.samples = event.samples[-100:]
                    event.confidence_scores = event.confidence_scores[-100:]

                events.append(
                    {
                        "type": "updated",
                        "signature_id": best_id,
                        "appliance": best_sig.name,
                        "duration_s": event.duration_s(),
                        "avg_w": event.avg_w,
                        "confidence": event.avg_confidence(),
                    }
                )

        # Check for events that should be closed
        to_remove = []
        for sig_id, event in list(self.active_events.items()):
            # If not the best match this second, see if it's timed out
            if sig_id != best_id:
                idle_for = ts - event.last_update_ts
                if idle_for >= self.debounce_seconds:
                    duration = event.duration_s()
                    if duration >= event.signature.min_duration_s:
                        events.append(
                            {
                                "type": "stopped",
                                "signature_id": sig_id,
                                "appliance": event.signature.name,
                                "start_ts": event.start_ts,
                                "end_ts": ts,
                                "duration_s": duration,
                                "avg_w": event.avg_w,
                                "peak_w": event.peak_w,
                                "confidence": event.avg_confidence(),
                            }
                        )
                    to_remove.append(sig_id)

        for sig_id in to_remove:
            self.active_events.pop(sig_id, None)

        return events


class ApplianceTrainer:
    """Training helper for enhanced signatures."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.extractor = FeatureExtractor()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def train_from_samples(self, name: str, samples: List[Tuple[float, float]]) -> Optional[ApplianceSignature]:
        if len(samples) < 30:
            return None

        baseline = self.extractor.baseline_from_samples(samples)
        profile = self.extractor.analyze_power_profile(samples, baseline)
        mean = profile["mean"]
        if mean < 30:
            return None

        duration = samples[-1][0] - samples[0][0]
        min_duration = max(30, int(duration * 0.5))
        max_duration = int(duration * 3)

        sig = ApplianceSignature(
            id=0,
            name=name,
            baseline_w=baseline,
            startup_w=profile["max"],
            running_w=mean,
            shutdown_w=0.0,
            std_dev_w=profile["std"],
            min_duration_s=min_duration,
            max_duration_s=max_duration,
            has_startup_spike=profile["startup_spike"] > mean * 0.2,
            has_cycling=profile["has_cycling"],
            cycle_period_s=profile["cycle_period"],
            created_ts=int(time.time()),
            sample_count=len(samples),
        )

        conn = self._conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO appliance_signatures_v2
                (name, baseline_w, startup_w, running_w, shutdown_w, std_dev_w,
                 min_duration_s, max_duration_s, has_startup_spike, has_cycling,
                 cycle_period_s, created_ts, sample_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sig.name,
                    sig.baseline_w,
                    sig.startup_w,
                    sig.running_w,
                    sig.shutdown_w,
                    sig.std_dev_w,
                    sig.min_duration_s,
                    sig.max_duration_s,
                    int(sig.has_startup_spike),
                    int(sig.has_cycling),
                    sig.cycle_period_s,
                    sig.created_ts,
                    sig.sample_count,
                ),
            )
            conn.commit()
            sig.id = cur.lastrowid
        finally:
            conn.close()

        return sig


# ----------------- Smart analytics functions -----------------

def predict_monthly_cost():
    """Predict end-of-month bill from current usage."""
    today = date.today()
    first = date(today.year, today.month, 1)
    start_ts = datetime(first.year, first.month, 1).timestamp()
    now_ts = time.time()

    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT SUM(cost) as total FROM samples WHERE ts>=? AND ts<?",
            (start_ts, now_ts),
        )
        row = cur.fetchone()
        spent = row["total"] or 0.0
    finally:
        conn.close()

    days_elapsed = (today - first).days + 1
    if days_elapsed <= 0:
        return {"predicted_monthly": 0, "spent_so_far": 0, "confidence": "low"}

    # Days in month
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)

    days_in_month = (next_month - first).days
    daily_avg = spent / days_elapsed
    predicted = daily_avg * days_in_month

    if days_elapsed > 7:
        confidence = "high"
    elif days_elapsed > 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "predicted_monthly": round(predicted, 2),
        "spent_so_far": round(spent, 2),
        "daily_average": round(daily_avg, 2),
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "confidence": confidence,
    }


def calculate_tou_savings(appliance_name: str, avg_w: float, duration_s: float):
    """Estimate savings by shifting this run to off-peak."""
    now = time.time()
    _, current_period, current_rate = get_tou_info(now)

    dt = datetime.fromtimestamp(now)
    off_peak_rate = None
    hours_until = 0

    for h in range(24):
        test_dt = dt + timedelta(hours=h)
        test_ts = test_dt.timestamp()
        _, period, rate = get_tou_info(test_ts)
        if period == "off_peak":
            off_peak_rate = rate
            hours_until = h
            break

    if off_peak_rate is None or current_period == "off_peak":
        return None

    energy_kwh = (avg_w * duration_s) / (3600.0 * 1000.0)
    current_cost = energy_kwh * current_rate
    off_cost = energy_kwh * off_peak_rate
    savings = current_cost - off_cost

    if savings < 0.10:
        return None

    return {
        "appliance": appliance_name,
        "savings": round(savings, 2),
        "hours_until_off_peak": hours_until,
        "current_period": current_period,
    }


def detect_anomalies():
    """Detect high baseline 'always-on' power vs previous week."""
    now = time.time()
    if now - anomaly_state["last_check"] < 300:
        return

    anomaly_state["last_check"] = now
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row

        # Today's baseline between 1–5am
        today_start = datetime(today.year, today.month, today.day).timestamp()
        cur = conn.execute(
            """
            SELECT AVG(power_w) as avg
            FROM samples
            WHERE ts>=? AND ts<? AND
                  CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER)
                  BETWEEN 1 AND 5
            """,
            (today_start, now),
        )
        row = cur.fetchone()
        today_baseline = row["avg"] if row and row["avg"] is not None else None

        week_start = datetime(week_ago.year, week_ago.month, week_ago.day).timestamp()
        yesterday_end = datetime(yesterday.year, yesterday.month, yesterday.day).timestamp()
        cur = conn.execute(
            """
            SELECT AVG(power_w) as avg
            FROM samples
            WHERE ts>=? AND ts<? AND
                  CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER)
                  BETWEEN 1 AND 5
            """,
            (week_start, yesterday_end),
        )
        row = cur.fetchone()
        week_baseline = row["avg"] if row and row["avg"] is not None else None

        alerts = []

        if today_baseline and week_baseline and today_baseline > week_baseline * 1.3:
            msg = (
                f"Always-on power is {int(today_baseline - week_baseline)}W higher "
                f"than usual. Check for devices left on."
            )
            alert = {
                "type": "high_baseline",
                "message": msg,
                "severity": "warning",
            }
            alerts.append(alert)
            conn.execute(
                """
                INSERT INTO anomaly_alerts(ts, type, message, severity, created_ts)
                VALUES(?,?,?,?,?)
                """,
                (now, alert["type"], alert["message"], alert["severity"], now),
            )
            conn.commit()

        anomaly_state["alerts"] = alerts
    finally:
        conn.close()


def get_appliance_costs_breakdown(days: int = 30):
    since = time.time() - days * 24 * 3600
    conn = get_db_connection()
    try:
        cur = conn.execute(
            """
            SELECT appliance_name, SUM(cost) as total_cost,
                   SUM(energy_kwh) as total_kwh, COUNT(*) as runs
            FROM appliance_events
            WHERE start_ts>=?
            GROUP BY appliance_name
            ORDER BY total_cost DESC
            """,
            (since,),
        )
        return [
            {
                "name": r["appliance_name"],
                "cost": round(r["total_cost"] or 0.0, 2),
                "kwh": round(r["total_kwh"] or 0.0, 2),
                "runs": r["runs"],
            }
            for r in cur
        ]
    finally:
        conn.close()


def detect_appliance_degradation(appliance_name: str):
    """Check if an appliance is using significantly more kWh over last 30 days."""
    conn = get_db_connection()
    try:
        since = time.time() - 30 * 24 * 3600
        cur = conn.execute(
            """
            SELECT energy_kwh, start_ts
            FROM appliance_events
            WHERE appliance_name=? AND start_ts>=?
            ORDER BY start_ts
            """,
            (appliance_name, since),
        )
        events = [{"kwh": r["energy_kwh"], "ts": r["start_ts"]} for r in cur]
        if len(events) < 5:
            return None

        mid = len(events) // 2
        first_avg = sum(e["kwh"] for e in events[:mid]) / mid
        second_avg = sum(e["kwh"] for e in events[mid:]) / (len(events) - mid)

        if second_avg > first_avg * 1.2:
            inc_pct = (second_avg - first_avg) / first_avg * 100.0
            return {
                "appliance_name": appliance_name,
                "increase_pct": round(inc_pct, 1),
                "message": f"{appliance_name} is using {int(inc_pct)}% more energy than before",
            }
        return None
    finally:
        conn.close()


def track_energy_goal():
    """Monthly kWh goal tracking."""
    goal_str = get_setting("goal_monthly_kwh", "0")
    try:
        goal_kwh = float(goal_str)
    except (TypeError, ValueError):
        goal_kwh = 0.0

    if goal_kwh <= 0:
        return None

    today = date.today()
    month_start_dt = date(today.year, today.month, 1)
    month_start_ts = datetime(month_start_dt.year, month_start_dt.month, month_start_dt.day).timestamp()

    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT SUM(energy_kwh) as used FROM samples WHERE ts>=?",
            (month_start_ts,),
        )
        used = cur.fetchone()["used"] or 0.0
    finally:
        conn.close()

    days_elapsed = (today - month_start_dt).days + 1
    if today.month == 12:
        days_in_month = 31
    else:
        days_in_month = (date(today.year, today.month + 1, 1) - month_start_dt).days

    projected = (used / days_elapsed) * days_in_month if days_elapsed > 0 else 0.0
    pct_to_goal = (used / goal_kwh) * 100.0 if goal_kwh > 0 else 0.0

    return {
        "goal_kwh": round(goal_kwh, 1),
        "used_kwh": round(used, 1),
        "projected_kwh": round(projected, 1),
        "pct_to_goal": round(pct_to_goal, 1),
        "on_track": projected <= goal_kwh,
    }


def predict_peak_demand():
    """Warn if you're close to today's peak."""
    with state_lock:
        if not recent_samples:
            return None
        _, current_w = recent_samples[-1]
    if current_w is None:
        return None

    today = date.today()
    day_start_ts = datetime(today.year, today.month, today.day).timestamp()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT MAX(power_w) as max_today FROM samples WHERE ts>=?",
            (day_start_ts,),
        )
        row = cur.fetchone()
        max_today = row["max_today"] or 0.0
    finally:
        conn.close()

    if max_today <= 0:
        return None

    if current_w > max_today * 0.9:
        return {
            "warning": True,
            "current_w": int(current_w),
            "todays_peak": int(max_today),
            "message": f"You're at {int(current_w)}W, close to today's peak of {int(max_today)}W!",
        }
    return None


def detect_vacation_mode():
    """Detect if usage pattern suggests you're away."""
    now = time.time()
    recent_start = now - 3 * 24 * 3600
    month_ago = now - 30 * 24 * 3600

    conn = get_db_connection()
    try:
        # Recent avg power
        cur = conn.execute(
            "SELECT AVG(power_w) as avg FROM samples WHERE ts>=?",
            (recent_start,),
        )
        recent_avg = cur.fetchone()["avg"] or 0.0

        # Baseline at night in last month
        cur = conn.execute(
            """
            SELECT AVG(power_w) as avg FROM samples
            WHERE ts>=? AND ts<? AND
                  CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER)
                  BETWEEN 1 AND 5
            """,
            (month_ago, recent_start),
        )
        baseline = cur.fetchone()["avg"] or 0.0
    finally:
        conn.close()

    if baseline > 0 and recent_avg < baseline * 1.2:
        return {
            "likely_away": True,
            "current_avg": int(recent_avg),
            "message": "Usage pattern suggests you're away. Baseline power looks normal.",
        }
    return None


def simulate_different_rate_plan(plan_rates: Dict[str, Dict[str, float]]):
    """Recalculate last month's bill under a different TOU plan."""
    today = date.today()
    if today.month == 1:
        last_month = date(today.year - 1, 12, 1)
    else:
        last_month = date(today.year, today.month - 1, 1)

    start_ts = datetime(last_month.year, last_month.month, 1).timestamp()
    if last_month.month == 12:
        end_ts = datetime(last_month.year + 1, 1, 1).timestamp()
    else:
        end_ts = datetime(last_month.year, last_month.month + 1, 1).timestamp()

    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT energy_kwh, season, tou_period FROM samples WHERE ts>=? AND ts<?",
            (start_ts, end_ts),
        )
        actual_cost = 0.0
        simulated_cost = 0.0
        for row in cur:
            kwh = row["energy_kwh"] or 0.0
            season = row["season"]
            period = row["tou_period"]
            actual_rate = RATES.get(season, {}).get(period, 0.10)
            sim_rate = plan_rates.get(season, {}).get(period, actual_rate)
            actual_cost += kwh * actual_rate
            simulated_cost += kwh * sim_rate
    finally:
        conn.close()

    diff = simulated_cost - actual_cost
    savings_pct = ((actual_cost - simulated_cost) / actual_cost * 100.0) if actual_cost > 0 else 0.0

    return {
        "actual_cost": round(actual_cost, 2),
        "simulated_cost": round(simulated_cost, 2),
        "difference": round(diff, 2),
        "savings_pct": round(savings_pct, 1),
    }


def calculate_carbon_footprint():
    """Convert this month's kWh to CO2 emissions."""
    ONTARIO_CO2_PER_KWH = 0.040  # kg CO2 per kWh

    today = date.today()
    start = datetime(today.year, today.month, 1).timestamp()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT SUM(energy_kwh) as total FROM samples WHERE ts>=?",
            (start,),
        )
        kwh = cur.fetchone()["total"] or 0.0
    finally:
        conn.close()

    co2_kg = kwh * ONTARIO_CO2_PER_KWH
    trees = co2_kg / 21.0
    km_driven = co2_kg / 0.192

    return {
        "kwh": round(kwh, 1),
        "co2_kg": round(co2_kg, 1),
        "trees_equivalent": round(trees, 1),
        "km_driven_equivalent": round(km_driven, 0),
    }


def forecast_tomorrow():
    """Predict tomorrow's cost using same weekday over last few weeks."""
    tomorrow = date.today() + timedelta(days=1)
    conn = get_db_connection()
    costs = []
    try:
        for weeks_ago in range(1, 5):
            target_date = tomorrow - timedelta(weeks=weeks_ago)
            start = datetime(target_date.year, target_date.month, target_date.day).timestamp()
            end = start + 24 * 3600
            cur = conn.execute(
                "SELECT SUM(cost) as total FROM samples WHERE ts>=? AND ts<?",
                (start, end),
            )
            total = cur.fetchone()["total"] or 0.0
            if total > 0:
                costs.append(total)
    finally:
        conn.close()

    if not costs:
        return None

    avg_cost = sum(costs) / len(costs)
    return {
        "predicted_cost": round(avg_cost, 2),
        "day": tomorrow.strftime("%A"),
        "confidence": "high" if len(costs) >= 3 else "medium",
    }


def run_auto_discovery_analysis():
    """Cluster appliance_events during discovery window."""
    if not discovery_state["active"] or not discovery_state["start_ts"]:
        return []

    start_ts = discovery_state["start_ts"]
    now = time.time()
    if now - start_ts < 3600:
        return []

    conn = get_db_connection()
    try:
        cur = conn.execute(
            """
            SELECT start_ts, end_ts, energy_kwh, cost
            FROM appliance_events
            WHERE start_ts>=?
            """,
            (start_ts,),
        )
        events = []
        for row in cur:
            duration = row["end_ts"] - row["start_ts"]
            events.append(
                {
                    "start": row["start_ts"],
                    "duration_min": int(duration / 60.0),
                    "kwh": row["energy_kwh"],
                    "cost": row["cost"],
                }
            )

        clusters: Dict[Tuple[int, float], List[Dict]] = defaultdict(list)
        for ev in events:
            key = (round(ev["duration_min"] / 10) * 10, round(ev["kwh"], 1))
            clusters[key].append(ev)

        suggestions = []
        for (dur, kwh), evs in clusters.items():
            if len(evs) >= 2:
                avg_cost = sum(e["cost"] for e in evs) / len(evs)
                suggestions.append(
                    {
                        "description": f"~{dur} min appliance using {kwh} kWh",
                        "occurrences": len(evs),
                        "avg_cost_per_use": round(avg_cost, 2),
                        "suggested_name": guess_appliance_name(dur, kwh),
                    }
                )
        return suggestions
    finally:
        conn.close()


def guess_appliance_name(duration_min: int, kwh: float) -> str:
    if duration_min <= 0:
        return "Unknown Appliance"
    avg_w = (kwh * 1000.0) / (duration_min / 60.0)
    if avg_w > 2000:
        return "EV Charger or Dryer"
    elif avg_w > 1200 and duration_min < 60:
        return "Dryer or Oven"
    elif avg_w > 800 and duration_min > 60:
        return "Dishwasher"
    elif avg_w > 500 and duration_min < 90:
        return "Washer"
    else:
        return "Unknown Appliance"


def send_notification(title: str, message: str, severity: str = "info"):
    webhook_url = get_setting("webhook_url")
    if not webhook_url:
        return
    try:
        requests.post(
            webhook_url,
            json={"title": title, "message": message, "severity": severity},
            timeout=5,
        )
    except Exception:
        pass


def get_voice_summary() -> str:
    pred = predict_monthly_cost()
    return (
        f"Your predicted bill this month is ${pred['predicted_monthly']:.2f}. "
        f"You're currently spending about ${pred['daily_average']:.2f} per day. "
        f"You've spent ${pred['spent_so_far']:.2f} so far this month."
    )


# ----------------- Sensor polling & storage -----------------

def read_neurio_power_w() -> Optional[float]:
    try:
        resp = requests.get(NEURIO_URL, timeout=2)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    power_w = None
    for ch in data.get("channels", []):
        if ch.get("type") == "CONSUMPTION":
            try:
                power_w = float(ch.get("p_W", 0.0))
            except Exception:
                power_w = None
            break
    return power_w


def store_aggregate(ts: float, avg_power_w: float):
    if avg_power_w is None:
        return

    interval = AGGREGATE_INTERVAL_SECONDS
    energy_kwh = avg_power_w * interval / 1000.0 / 3600.0
    season, period, rate = get_tou_info(ts)
    cost = energy_kwh * rate

    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO samples
            (ts, power_w, energy_kwh, cost, season, tou_period, rate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, avg_power_w, energy_kwh, cost, season, period, rate),
        )
        conn.commit()
    finally:
        conn.close()

    prune_old_data()
    detect_anomalies()


def store_detector_event(ev: Dict):
    """Persist an event from the enhanced detector and create insights."""
    name = ev["appliance"]
    start_ts = float(ev["start_ts"])
    end_ts = float(ev["end_ts"])
    avg_w = float(ev["avg_w"])
    duration_s = max(float(ev["duration_s"]), 1.0)
    confidence = float(ev.get("confidence", 0.0))

    energy_kwh = avg_w * duration_s / 1000.0 / 3600.0
    mid_ts = (start_ts + end_ts) / 2.0
    season, period, rate = get_tou_info(mid_ts)
    cost = energy_kwh * rate

    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO appliance_events
            (appliance_name, start_ts, end_ts, avg_w, energy_kwh, cost, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, start_ts, end_ts, avg_w, energy_kwh, cost, confidence),
        )
        conn.commit()
    finally:
        conn.close()

    anomaly_state["recent_events"].append(
        {
            "ts": end_ts,
            "appliance": name,
            "avg_w": avg_w,
            "duration_s": duration_s,
            "cost": cost,
            "confidence": confidence,
        }
    )

    savings = calculate_tou_savings(name, avg_w, duration_s)
    if savings:
        msg = (
            f"Running {savings['appliance']} now costs ${cost:.2f}. "
            f"Wait {savings['hours_until_off_peak']}h for off-peak "
            f"to save about ${savings['savings']:.2f}."
        )
        now = time.time()
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO insights(ts, type, message, potential_savings, created_ts)
                VALUES(?,?,?,?,?)
                """,
                (now, "tou_optimization", msg, savings["savings"], now),
            )
            conn.commit()
        finally:
            conn.close()

        send_notification("TOU optimization", msg, "info")


def poll_sensor_loop():
    global last_agg_ts, agg_buffer, enhanced_detector

    while True:
        start_loop = time.time()
        ts_now = start_loop
        power_w = read_neurio_power_w()

        with state_lock:
            recent_samples.append((ts_now, power_w))

        # Enhanced detection
        if enhanced_detector is not None:
            try:
                events = enhanced_detector.process_sample(ts_now, power_w)
                for ev in events:
                    if ev["type"] == "stopped":
                        store_detector_event(ev)
            except Exception:
                # do not crash loop
                pass

        # Aggregation buffer
        if power_w is not None:
            agg_buffer.append((ts_now, power_w))

        if last_agg_ts is None:
            last_agg_ts = ts_now

        if ts_now - last_agg_ts >= AGGREGATE_INTERVAL_SECONDS and agg_buffer:
            powers = [p for _, p in agg_buffer if p is not None]
            if powers:
                avg_power = sum(powers) / len(powers)
                mid_index = len(agg_buffer) // 2
                agg_ts = agg_buffer[mid_index][0]
                store_aggregate(agg_ts, avg_power)
            agg_buffer = []
            last_agg_ts = ts_now

        elapsed = time.time() - start_loop
        sleep_for = max(0.0, POLL_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_for)


# ----------------- API routes -----------------

@app.route("/api/live")
def api_live():
    with state_lock:
        if recent_samples:
            ts, power_w = recent_samples[-1]
        else:
            ts, power_w = time.time(), None

    # Today's totals
    today = date.today()
    day_start_ts = datetime(today.year, today.month, today.day).timestamp()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT SUM(energy_kwh) as kwh, SUM(cost) as cost FROM samples WHERE ts>=?",
            (day_start_ts,),
        )
        row = cur.fetchone()
        kwh = row["kwh"] or 0.0
        cost = row["cost"] or 0.0
    finally:
        conn.close()

    return jsonify(
        {
            "ts": ts,
            "ts_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "power_w": power_w if power_w is not None else 0.0,
            "today_kwh": round(kwh, 3),
            "today_cost": round(cost, 2),
            "error": None if power_w is not None else "No recent sample",
        }
    )


@app.route("/api/day")
def api_day():
    day_str = request.args.get("day")
    if day_str:
        dt_day = datetime.strptime(day_str, "%Y-%m-%d").date()
    else:
        dt_day = date.today()

    start_dt = datetime(dt_day.year, dt_day.month, dt_day.day)
    end_dt = start_dt + timedelta(days=1)
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    conn = get_db_connection()
    try:
        cur = conn.execute(
            """
            SELECT ts, power_w, energy_kwh, cost
            FROM samples
            WHERE ts>=? AND ts<?
            ORDER BY ts
            """,
            (start_ts, end_ts),
        )
        samples = [
            {
                "ts": row["ts"],
                "ts_str": datetime.fromtimestamp(row["ts"]).strftime("%H:%M"),
                "power_w": row["power_w"],
                "energy_kwh": row["energy_kwh"],
                "cost": row["cost"],
            }
            for row in cur
        ]
    finally:
        conn.close()

    return jsonify({"day": dt_day.isoformat(), "samples": samples})


@app.route("/api/appliances")
def api_appliances():
    days = int(request.args.get("days", "7"))
    since = time.time() - days * 24 * 3600

    conn = get_db_connection()
    try:
        cur = conn.execute(
            """
            SELECT appliance_name,
                   SUM(energy_kwh) as total_kwh,
                   SUM(cost) as total_cost,
                   COUNT(*) as runs
            FROM appliance_events
            WHERE start_ts>=?
            GROUP BY appliance_name
            ORDER BY total_cost DESC
            """,
            (since,),
        )
        rows = [
            {
                "name": r["appliance_name"],
                "kwh": round(r["total_kwh"] or 0.0, 2),
                "cost": round(r["total_cost"] or 0.0, 2),
                "runs": r["runs"],
            }
            for r in cur
        ]
    finally:
        conn.close()

    return jsonify({"since_days": days, "appliances": rows})


@app.route("/api/prediction")
def api_prediction():
    try:
        data = predict_monthly_cost()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/insights")
def api_insights():
    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT message, potential_savings FROM insights WHERE dismissed=0 "
            "ORDER BY ts DESC LIMIT 10"
        )
        insights = [
            {
                "message": r["message"],
                "potential_savings": r["potential_savings"],
            }
            for r in cur
        ]

        cur = conn.execute(
            "SELECT type, message, severity FROM anomaly_alerts "
            "WHERE acknowledged=0 ORDER BY ts DESC LIMIT 5"
        )
        anomalies = [
            {
                "type": r["type"],
                "message": r["message"],
                "severity": r["severity"],
            }
            for r in cur
        ]

        return jsonify({"insights": insights, "anomalies": anomalies})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/goals")
def api_goals():
    goal = track_energy_goal()
    footprint = calculate_carbon_footprint()
    forecast = forecast_tomorrow()
    vacation = detect_vacation_mode()
    peak = predict_peak_demand()
    return jsonify(
        {
            "goal": goal,
            "footprint": footprint,
            "tomorrow": forecast,
            "vacation_mode": vacation,
            "peak_warning": peak,
        }
    )


@app.route("/api/appliance_costs")
def api_appliance_costs():
    days = int(request.args.get("days", "30"))
    breakdown = get_appliance_costs_breakdown(days=days)
    return jsonify({"days": days, "appliances": breakdown})


@app.route("/api/voice_summary")
def api_voice_summary():
    return jsonify({"summary": get_voice_summary()})


# ----------------- HTML dashboard -----------------
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Neurio Local Power Monitor</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      color-scheme: dark light;
    }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background-color: #111827;
      color: #e5e7eb;
    }
    body.light {
      background-color: #f3f4f6;
      color: #111827;
    }
    .page {
      max-width: 1200px;
      margin: 0 auto;
      padding: 16px;
    }
    .top-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
    }
    h1 {
      font-size: 1.6rem;
      margin: 0;
    }
    .clock {
      font-size: 0.9rem;
      opacity: 0.8;
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    button, input, select {
      font-family: inherit;
    }
    .btn {
      border: 1px solid rgba(249, 250, 251, 0.1);
      padding: 6px 12px;
      border-radius: 999px;
      background: #1f2937;
      color: #e5e7eb;
      font-size: 0.85rem;
      cursor: pointer;
    }
    body.light .btn {
      background: #ffffff;
      color: #111827;
      border-color: #d1d5db;
    }
    .btn:hover {
      filter: brightness(1.1);
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .card {
      background: radial-gradient(circle at top left, rgba(59,130,246,0.3), transparent),
                  #020617;
      border-radius: 12px;
      padding: 12px;
      border: 1px solid rgba(148,163,184,0.3);
      box-shadow: 0 18px 45px rgba(15,23,42,0.8);
    }
    body.light .card {
      background: #ffffff;
      box-shadow: 0 10px 30px rgba(15,23,42,0.08);
      border-color: #e5e7eb;
    }
    .card-title {
      font-size: 0.9rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      opacity: 0.7;
      margin-bottom: 4px;
    }
    .card-main {
      font-size: 1.6rem;
      font-weight: 600;
      margin-bottom: 4px;
    }
    .card-sub {
      font-size: 0.8rem;
      opacity: 0.8;
    }
    .card-row {
      display: flex;
      justify-content: space-between;
      font-size: 0.8rem;
      margin-top: 4px;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 16px;
      font-size: 0.85rem;
    }
    .controls label {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    input[type="date"], input[type="number"], input[type="text"] {
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.6);
      background: transparent;
      color: inherit;
      font-size: 0.85rem;
    }
    body.light input[type="date"],
    body.light input[type="number"],
    body.light input[type="text"] {
      background: #ffffff;
      border-color: #d1d5db;
    }
    input[type="number"] {
      width: 80px;
    }
    .chart-block {
      margin-bottom: 18px;
      background: #020617;
      border-radius: 12px;
      padding: 12px;
      border: 1px solid rgba(148,163,184,0.3);
      box-shadow: 0 18px 45px rgba(15,23,42,0.8);
    }
    body.light .chart-block {
      background: #ffffff;
      border-color: #e5e7eb;
      box-shadow: 0 10px 30px rgba(15,23,42,0.08);
    }
    .chart-block h2 {
      font-size: 0.95rem;
      margin-top: 0;
      margin-bottom: 8px;
      opacity: 0.9;
    }
    canvas {
      max-height: 260px;
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="top-bar">
      <div>
        <h1>Neurio Local Power Monitor</h1>
        <div class="clock" id="clock"></div>
      </div>
      <div class="top-actions">
        <button class="btn" id="themeToggle">Toggle theme</button>
      </div>
    </div>

    <div class="cards">
      <div class="card" id="nowCard">
        <div class="card-title">Now</div>
        <div class="card-main"><span id="nowPower">0.00</span> kW</div>
        <div class="card-sub">
          <span id="nowTouLabel">Off peak</span>,
          <span id="nowSeason">season</span>
        </div>
        <div class="card-row">
          <span>Cost per hour</span>
          <span id="nowCostPerHour">$0.000</span>
        </div>
        <div class="card-row">
          <span>Rate</span>
          <span id="nowRate">$0.000 /kWh</span>
        </div>
      </div>

      <div class="card" id="dayCard">
        <div class="card-title">Selected day (24 h total)</div>
        <div class="card-main">
          <span id="dayKwh">0.00</span> kWh
        </div>
        <div class="card-sub">
          Total cost <span id="dayCost">$0.00</span>
        </div>
        <div class="card-row">
          <span>Max draw</span>
          <span id="dayMaxKw">0.00 kW</span>
        </div>
        <div class="card-row">
          <span>Always on</span>
          <span id="dayAlwaysOn">0 W</span>
        </div>
        <div class="card-row">
          <span>Load factor</span>
          <span id="dayLoadFactor">0.00</span>
        </div>
        <div class="card-row">
          <span>Budget</span>
          <span id="dayBudget">â€“</span>
        </div>
        <div class="card-row">
          <span>Hit time</span>
          <span id="dayBudgetHit">Not set</span>
        </div>
      </div>

      <div class="card" id="monthCard">
        <div class="card-title">Month overview</div>
        <div class="card-main">
          <span id="monthKwh">0.00</span> kWh
        </div>
        <div class="card-sub">
          Cost <span id="monthCost">$0.00</span>
        </div>
        <div class="card-row">
          <span>Vs last month</span>
          <span id="monthDeltaKwh">0.00 kWh</span>
        </div>
        <div class="card-row">
          <span>Cost change</span>
          <span id="monthDeltaCost">$0.00</span>
        </div>
        <div class="card-row">
          <span>Always-on change</span>
          <span id="alwaysOnChange">0 W</span>
        </div>
      </div>

      <div class="card" id="weekCard">
        <div class="card-title">This week vs last</div>
        <div class="card-main">
          <span id="weekKwh">0.00</span> kWh
        </div>
        <div class="card-sub">
          Cost <span id="weekCost">$0.00</span>
        </div>
        <div class="card-row">
          <span>Vs last week</span>
          <span id="weekDeltaKwh">0.00 kWh</span>
        </div>
        <div class="card-row">
          <span>Cost change</span>
          <span id="weekDeltaCost">$0.00</span>
        </div>
      </div>

      <div class="card" id="predictionCard">
        <div class="card-title">Bill prediction</div>
        <div class="card-main">
          <span id="predMonthly">$0.00</span>
        </div>
        <div class="card-sub">
          Spent so far <span id="predSpent">$0.00</span>
        </div>
        <div class="card-row">
          <span>Daily average</span>
          <span id="predDaily">$0.00 / day</span>
        </div>
        <div class="card-row">
          <span>Confidence</span>
          <span id="predConfidence">low</span>
        </div>
      </div>
    </div>

    <div class="controls">
      <label>
        Day:
        <input type="date" id="dayPicker">
      </label>
      <button class="btn" id="exportCsvBtn">Export CSV</button>
    </div>

    <div class="chart-block">
      <h2>Live power, last 60 seconds</h2>
      <canvas id="liveChart"></canvas>
    </div>

    <div class="chart-block">
      <h2>Selected day, 2 minute samples (24h spikes view)</h2>
      <canvas id="dayChart"></canvas>
    </div>

    <div class="chart-block">
      <h2>Selected day by hour (kWh & $)</h2>
      <canvas id="hourlyChart"></canvas>
    </div>

    <div class="chart-block">
      <h2>Appliance cost breakdown (last 30 days)</h2>
      <canvas id="applianceCostChart"></canvas>
      <div class="card-sub" id="applianceCostSubtitle"></div>
    </div>

    <div class="chart-block">
      <h2>Smart insights and alerts</h2>
      <div id="insightsContainer" style="font-size:0.85rem;"></div>
    </div>

    <div class="chart-block">
      <h2>Goals, footprint and forecast</h2>
      <div class="card-row">
        <span>Energy goal</span>
        <span id="goalSummary">No goal set.</span>
      </div>
      <div class="card-row">
        <span>Carbon footprint (this month)</span>
        <span id="co2Summary">0 kg CO2</span>
      </div>
      <div class="card-row">
        <span>Tomorrow forecast</span>
        <span id="forecastSummary">No data yet.</span>
      </div>
      <div class="card-row">
        <span>Vacation mode</span>
        <span id="vacationSummary">Normal usage.</span>
      </div>
      <div class="card-row">
        <span>Peak warning</span>
        <span id="peakSummary">No warning.</span>
      </div>
    </div>

    <div class="chart-block">
      <h2>TOU rates ($/kWh)</h2>
      <div style="display:flex; flex-wrap:wrap; gap:16px; font-size:0.85rem;">
        <div style="min-width:220px;">
          <div class="card-title">Winter</div>
          <div class="controls" style="margin-bottom:4px;">
            <label>Off-peak
              <input type="number" step="0.001" id="rateWinterOffPeak">
            </label>
          </div>
          <div class="controls" style="margin-bottom:4px;">
            <label>Mid-peak
              <input type="number" step="0.001" id="rateWinterMidPeak">
            </label>
          </div>
          <div class="controls">
            <label>On-peak
              <input type="number" step="0.001" id="rateWinterOnPeak">
            </label>
          </div>
        </div>
        <div style="min-width:220px;">
          <div class="card-title">Summer</div>
          <div class="controls" style="margin-bottom:4px;">
            <label>Off-peak
              <input type="number" step="0.001" id="rateSummerOffPeak">
            </label>
          </div>
          <div class="controls" style="margin-bottom:4px;">
            <label>Mid-peak
              <input type="number" step="0.001" id="rateSummerMidPeak">
            </label>
          </div>
          <div class="controls">
            <label>On-peak
              <input type="number" step="0.001" id="rateSummerOnPeak">
            </label>
          </div>
        </div>
      </div>
      <div style="margin-top:8px; font-size:0.85rem;">
        <label>Daily budget ($)
          <input type="number" step="0.01" id="dailyBudgetInput">
        </label>
      </div>
      <div style="margin-top:8px; display:flex; flex-wrap:wrap; gap:8px;">
        <button class="btn" id="saveRatesBtn">Save rates</button>
        <button class="btn" id="saveBudgetBtn">Save budget</button>
        <button class="btn" id="presetTouBtn">Preset: ON TOU 2025</button>
      </div>
    </div>

    <div class="chart-block">
      <h2>Appliance training and discovery</h2>
      <div class="controls">
        <label>Appliance name:
          <input type="text" id="applianceName" placeholder="Dryer, oven, etc.">
        </label>
        <button class="btn" id="startTrainingBtn">Start training</button>
        <button class="btn" id="stopTrainingBtn">Stop training</button>
      </div>
      <div class="card-sub" id="trainingStatus">Not training.</div>
      <hr style="margin:10px 0; border-color:rgba(148,163,184,0.4);">
      <div class="controls">
        <button class="btn" id="startDiscoveryBtn">Start discovery</button>
        <button class="btn" id="stopDiscoveryBtn">Stop discovery</button>
      </div>
      <div class="card-sub" id="discoveryStatus">Discovery inactive.</div>
      <ul id="discoveryList" style="font-size:0.8rem; padding-left:18px; margin-top:6px;"></ul>
    </div>

    <div class="chart-block">
      <h2>Appliances & recent events</h2>
      <div style="display:flex; flex-wrap:wrap; gap:16px;">
        <div style="flex:1; min-width:220px;">
          <div class="card-title">Known appliances</div>
          <ul id="applianceList" style="font-size:0.8rem; padding-left:18px; margin-top:6px;"></ul>
        </div>
        <div style="flex:2; min-width:260px;">
          <div class="card-title">Recent events (last 24 h)</div>
          <div id="applianceEvents" style="font-size:0.8rem; max-height:180px; overflow:auto;"></div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const clockEl = document.getElementById('clock');
    const themeToggle = document.getElementById('themeToggle');
    const dayPicker = document.getElementById('dayPicker');
    const exportBtn = document.getElementById('exportCsvBtn');

    const nowPowerEl = document.getElementById('nowPower');
    const nowTouLabelEl = document.getElementById('nowTouLabel');
    const nowSeasonEl = document.getElementById('nowSeason');
    const nowCostPerHourEl = document.getElementById('nowCostPerHour');
    const nowRateEl = document.getElementById('nowRate');

    const dayKwhEl = document.getElementById('dayKwh');
    const dayCostEl = document.getElementById('dayCost');
    const dayMaxKwEl = document.getElementById('dayMaxKw');
    const dayAlwaysOnEl = document.getElementById('dayAlwaysOn');
    const dayLoadFactorEl = document.getElementById('dayLoadFactor');
    const dayBudgetEl = document.getElementById('dayBudget');
    const dayBudgetHitEl = document.getElementById('dayBudgetHit');

    const monthKwhEl = document.getElementById('monthKwh');
    const monthCostEl = document.getElementById('monthCost');
    const monthDeltaKwhEl = document.getElementById('monthDeltaKwh');
    const monthDeltaCostEl = document.getElementById('monthDeltaCost');
    const alwaysOnChangeEl = document.getElementById('alwaysOnChange');

    const weekKwhEl = document.getElementById('weekKwh');
    const weekCostEl = document.getElementById('weekCost');
    const weekDeltaKwhEl = document.getElementById('weekDeltaKwh');
    const weekDeltaCostEl = document.getElementById('weekDeltaCost');

    const predMonthlyEl = document.getElementById('predMonthly');
    const predSpentEl = document.getElementById('predSpent');
    const predDailyEl = document.getElementById('predDaily');
    const predConfidenceEl = document.getElementById('predConfidence');

    const rateWinterOffPeakEl = document.getElementById('rateWinterOffPeak');
    const rateWinterMidPeakEl = document.getElementById('rateWinterMidPeak');
    const rateWinterOnPeakEl = document.getElementById('rateWinterOnPeak');
    const rateSummerOffPeakEl = document.getElementById('rateSummerOffPeak');
    const rateSummerMidPeakEl = document.getElementById('rateSummerMidPeak');
    const rateSummerOnPeakEl = document.getElementById('rateSummerOnPeak');
    const saveRatesBtn = document.getElementById('saveRatesBtn');

    const dailyBudgetInput = document.getElementById('dailyBudgetInput');
    const saveBudgetBtn = document.getElementById('saveBudgetBtn');
    const presetTouBtn = document.getElementById('presetTouBtn');

    const applianceNameEl = document.getElementById('applianceName');
    const startTrainingBtn = document.getElementById('startTrainingBtn');
    const stopTrainingBtn = document.getElementById('stopTrainingBtn');
    const trainingStatusEl = document.getElementById('trainingStatus');
    const applianceListEl = document.getElementById('applianceList');
    const applianceEventsEl = document.getElementById('applianceEvents');

    const startDiscoveryBtn = document.getElementById('startDiscoveryBtn');
    const stopDiscoveryBtn = document.getElementById('stopDiscoveryBtn');
    const discoveryStatusEl = document.getElementById('discoveryStatus');
    const discoveryListEl = document.getElementById('discoveryList');

    const applianceCostSubtitleEl = document.getElementById('applianceCostSubtitle');

    const insightsContainerEl = document.getElementById('insightsContainer');
    const goalSummaryEl = document.getElementById('goalSummary');
    const co2SummaryEl = document.getElementById('co2Summary');
    const forecastSummaryEl = document.getElementById('forecastSummary');
    const vacationSummaryEl = document.getElementById('vacationSummary');
    const peakSummaryEl = document.getElementById('peakSummary');

    function updateClock() {
      const now = new Date();
      clockEl.textContent = now.toLocaleString();
    }
    setInterval(updateClock, 1000);
    updateClock();

    themeToggle.addEventListener('click', () => {
      document.body.classList.toggle('light');
    });

    const liveCtx = document.getElementById('liveChart').getContext('2d');
    const dayCtx = document.getElementById('dayChart').getContext('2d');
    const hourlyCtx = document.getElementById('hourlyChart').getContext('2d');
    const applianceCostCtx = document.getElementById('applianceCostChart').getContext('2d');

    const liveData = {
      labels: [],
      datasets: [{
        label: 'kW',
        data: [],
        tension: 0.2,
        pointRadius: 0
      }]
    };

    const liveChart = new Chart(liveCtx, {
      type: 'line',
      data: liveData,
      options: {
        animation: false,
        scales: {
          x: {
            display: false
          },
          y: {
            beginAtZero: true
          }
        },
        plugins: {
          legend: { display: false }
        }
      }
    });

    const dayChart = new Chart(dayCtx, {
      type: 'line',
      data: {
        labels: [],
        datasets: [{
          label: 'kW',
          data: [],
          tension: 0.2,
          pointRadius: 0
        }]
      },
      options: {
        scales: {
          x: {
            ticks: {
              maxRotation: 0,
              autoSkip: true
            }
          },
          y: {
            beginAtZero: true
          }
        },
        plugins: {
          legend: { display: false }
        }
      }
    });

    const hourlyChart = new Chart(hourlyCtx, {
      type: 'bar',
      data: {
        labels: [],
        datasets: [
          {
            label: 'kWh',
            data: [],
            yAxisID: 'yKwh'
          },
          {
            label: '$',
            data: [],
            yAxisID: 'yCost'
          }
        ]
      },
      options: {
        scales: {
          x: {
            ticks: {
              maxRotation: 0,
              autoSkip: false
            }
          },
          yKwh: {
            beginAtZero: true,
            position: 'left',
            title: { display: true, text: 'kWh' }
          },
          yCost: {
            beginAtZero: true,
            position: 'right',
            grid: { drawOnChartArea: false },
            title: { display: true, text: '$ per hour' }
          }
        },
        plugins: {
          legend: { display: true }
        }
      }
    });

    const applianceCostChart = new Chart(applianceCostCtx, {
      type: 'pie',
      data: {
        labels: [],
        datasets: [{
          label: 'Cost ($)',
          data: []
        }]
      },
      options: {
        plugins: {
          legend: { display: true }
        }
      }
    });

    function refreshLive() {
      fetch('/api/live')
        .then(r => r.json())
        .then(data => {
          const tsMs = data.ts * 1000;
          const dt = new Date(tsMs);
          const label = dt.toLocaleTimeString();

          const kW = data.power_w / 1000.0;
          liveData.labels.push(label);
          liveData.datasets[0].data.push(kW);

          if (liveData.labels.length > 60) {
            liveData.labels.shift();
            liveData.datasets[0].data.shift();
          }
          liveChart.update('none');

          nowPowerEl.textContent = kW.toFixed(2);
          nowTouLabelEl.textContent = data.tou_period.replace('_', ' ');
          nowSeasonEl.textContent = data.season;
          nowCostPerHourEl.textContent = '$' + (data.cost_per_hour || 0).toFixed(3);

          const rateDollar = data.rate_per_kwh != null
            ? data.rate_per_kwh
            : (data.rate_cents || 0) / 100.0;
          nowRateEl.textContent = '$' + rateDollar.toFixed(3) + ' /kWh';
        })
        .catch(err => {
          console.error('live error', err);
        });
    }

    function loadHistoryForDay(dayStr) {
      const url = dayStr ? ('/api/history?day=' + dayStr) : '/api/history';
      fetch(url)
        .then(r => r.json())
        .then(data => {
          const samples = data.samples || [];
          const hourly = data.hourly || [];
          const daily = data.daily_summary || {};
          const totals = data.totals || {};
          const budget = data.budget_info || {};

          const dayLabels = samples.map(s => (s.ts_str || '').slice(11, 16));
          const dayValues = samples.map(s => s.power_w / 1000.0);

          dayChart.data.labels = dayLabels;
          dayChart.data.datasets[0].data = dayValues;
          dayChart.update();

          const hLabels = hourly.map(h => {
            const dt = new Date(h.ts_hour * 1000);
            return dt.getHours().toString().padStart(2, '0') + ':00';
          });
          const hKwh = hourly.map(h => h.energy_kwh || 0);
          const hCost = hourly.map(h => h.cost || 0);

          hourlyChart.data.labels = hLabels;
          hourlyChart.data.datasets[0].data = hKwh;
          hourlyChart.data.datasets[1].data = hCost;
          hourlyChart.update();

          const total_kwh = daily.total_kwh != null ? daily.total_kwh : (totals.energy_kwh || 0);
          const total_cost = daily.total_cost != null ? daily.total_cost : (totals.cost || 0);

          dayKwhEl.textContent = total_kwh.toFixed(2);
          dayCostEl.textContent = '$' + total_cost.toFixed(2);
          dayMaxKwEl.textContent = (daily.max_kw || 0).toFixed(2) + ' kW';
          dayAlwaysOnEl.textContent = Math.round(daily.always_on_w || 0) + ' W';
          dayLoadFactorEl.textContent = (daily.load_factor || 0).toFixed(2);

          const budgetVal = budget.budget != null ? budget.budget : 0;
          const spent = budget.spent != null ? budget.spent : total_cost;

          if (budgetVal > 0) {
            dayBudgetEl.textContent = '$' + budgetVal.toFixed(2) +
              ' (' + spent.toFixed(2) + ' used)';
            if (budget.hit_str) {
              const t = budget.hit_str.slice(11, 16);
              dayBudgetHitEl.textContent = '$' + budgetVal.toFixed(2) + ' reached at ' + t;
            } else {
              dayBudgetHitEl.textContent = 'Not reached';
            }
          } else {
            dayBudgetEl.textContent = 'â€“';
            dayBudgetHitEl.textContent = 'Not set';
          }
        })
        .catch(err => {
          console.error('history error', err);
        });
    }

    function loadMonthlyOverview() {
      fetch('/api/monthly_overview')
        .then(r => r.json())
        .then(data => {
          const thisM = data.this_month || {kwh: 0, cost: 0};

          monthKwhEl.textContent = (thisM.kwh || 0).toFixed(2);
          monthCostEl.textContent = '$' + (thisM.cost || 0).toFixed(2);

          const deltaKwh = data.delta_kwh || 0;
          const deltaCost = data.delta_cost || 0;

          let deltaKwhText = (deltaKwh >= 0 ? '+' : '') + deltaKwh.toFixed(2) + ' kWh';
          let deltaCostText = (deltaCost >= 0 ? '+' : '') + '$' + deltaCost.toFixed(2);

          monthDeltaKwhEl.textContent = deltaKwhText;
          monthDeltaCostEl.textContent = deltaCostText;

          const ao = data.always_on || {};
          const deltaW = ao.delta_w || 0;
          const text = (deltaW >= 0 ? '+' : 'âˆ’') + Math.abs(deltaW).toFixed(0) + ' W vs prior night';
          alwaysOnChangeEl.textContent = text;
        })
        .catch(err => {
          console.error('monthly error', err);
        });
    }

    function loadWeekOverview() {
      fetch('/api/week_overview')
        .then(r => r.json())
        .then(data => {
          const thisW = data.this_week || {kwh: 0, cost: 0};

          weekKwhEl.textContent = (thisW.kwh || 0).toFixed(2);
          weekCostEl.textContent = '$' + (thisW.cost || 0).toFixed(2);

          const deltaKwh = data.delta_kwh || 0;
          const deltaCost = data.delta_cost || 0;

          let dkText = (deltaKwh >= 0 ? '+' : '') + deltaKwh.toFixed(2) + ' kWh';
          let dcText = (deltaCost >= 0 ? '+' : '') + '$' + deltaCost.toFixed(2);

          weekDeltaKwhEl.textContent = dkText;
          weekDeltaCostEl.textContent = dcText;
        })
        .catch(err => {
          console.error('week error', err);
        });
    }

    function loadPrediction() {
      fetch('/api/prediction')
        .then(r => r.json())
        .then(data => {
          if (data.error) {
            predMonthlyEl.textContent = 'Error';
            predSpentEl.textContent = '$0.00';
            predDailyEl.textContent = '$0.00 / day';
            predConfidenceEl.textContent = 'low';
            return;
          }
          predMonthlyEl.textContent = '$' + (data.predicted_monthly || 0).toFixed(2);
          predSpentEl.textContent = '$' + (data.spent_so_far || 0).toFixed(2);
          predDailyEl.textContent = '$' + (data.daily_average || 0).toFixed(2) + ' / day';
          predConfidenceEl.textContent = data.confidence || 'low';
        })
        .catch(err => {
          console.error('prediction error', err);
        });
    }

    function loadRates() {
      fetch('/api/rates')
        .then(r => r.json())
        .then(data => {
          const rates = data.rates || {};
          const winter = rates.winter || {};
          const summer = rates.summer || {};
          rateWinterOffPeakEl.value = winter.off_peak != null ? winter.off_peak : '';
          rateWinterMidPeakEl.value = winter.mid_peak != null ? winter.mid_peak : '';
          rateWinterOnPeakEl.value = winter.on_peak != null ? winter.on_peak : '';
          rateSummerOffPeakEl.value = summer.off_peak != null ? summer.off_peak : '';
          rateSummerMidPeakEl.value = summer.mid_peak != null ? summer.mid_peak : '';
          rateSummerOnPeakEl.value = summer.on_peak != null ? summer.on_peak : '';
        })
        .catch(err => {
          console.error('rates error', err);
        });
    }

    function saveRates() {
      const payload = {
        rates: {
          winter: {
            off_peak: parseFloat(rateWinterOffPeakEl.value) || 0,
            mid_peak: parseFloat(rateWinterMidPeakEl.value) || 0,
            on_peak: parseFloat(rateWinterOnPeakEl.value) || 0
          },
          summer: {
            off_peak: parseFloat(rateSummerOffPeakEl.value) || 0,
            mid_peak: parseFloat(rateSummerMidPeakEl.value) || 0,
            on_peak: parseFloat(rateSummerOnPeakEl.value) || 0
          }
        }
      };
      fetch('/api/rates', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      })
        .then(r => r.json())
        .then(() => {
          loadMonthlyOverview();
        })
        .catch(err => {
          console.error('save rates error', err);
        });
    }

    function loadSettings() {
      fetch('/api/settings')
        .then(r => r.json())
        .then(data => {
          if (data.daily_budget != null) {
            dailyBudgetInput.value = Number(data.daily_budget).toFixed(2);
          }
        })
        .catch(err => {
          console.error('settings error', err);
        });
    }

    function saveBudget() {
      const val = parseFloat(dailyBudgetInput.value) || 0;
      fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({daily_budget: val})
      })
        .then(r => r.json())
        .then(data => {
          if (data.daily_budget != null) {
            dailyBudgetInput.value = Number(data.daily_budget).toFixed(2);
          }
          const valDay = dayPicker.value;
          if (valDay) {
            loadHistoryForDay(valDay);
          }
        })
        .catch(err => {
          console.error('save settings error', err);
        });
    }

    function applyOntarioTouPreset() {
      rateWinterOffPeakEl.value = '0.098';
      rateWinterMidPeakEl.value = '0.157';
      rateWinterOnPeakEl.value = '0.203';
      rateSummerOffPeakEl.value = '0.098';
      rateSummerMidPeakEl.value = '0.157';
      rateSummerOnPeakEl.value = '0.203';
    }

    function loadAppliances() {
      fetch('/api/appliances')
        .then(r => r.json())
        .then(data => {
          const training = data.training || {};
          const signatures = data.signatures || [];
          const events = data.events || [];

          trainingStatusEl.textContent = training.active
            ? ('Training: ' + (training.name || '(unnamed)'))
            : 'Not training.';

          applianceListEl.innerHTML = '';
          if (signatures.length === 0) {
            applianceListEl.innerHTML = '<li>No appliances trained yet.</li>';
          } else {
            signatures.forEach(sig => {
              const li = document.createElement('li');
              li.textContent =
                sig.name + ' Â· Î”avg ' +
                (sig.avg_w || 0).toFixed(0) + ' W Â· Î”peak ' +
                (sig.peak_w || 0).toFixed(0) + ' W';
              applianceListEl.appendChild(li);
            });
          }

          let html = '';
          if (events.length === 0) {
            html = '<div>No events in last 24 hours.</div>';
          } else {
            events.forEach(ev => {
              const kwh = ev.energy_kwh || 0;
              const cost = ev.cost || 0;
              html += '<div style="margin-bottom:6px;">' +
                '<strong>' + ev.appliance_name + '</strong> Â· ' +
                ev.start_str + ' â†’ ' + ev.end_str +
                '<br>' +
                kwh.toFixed(2) + ' kWh, $' + cost.toFixed(2) +
                '</div>';
            });
          }
          applianceEventsEl.innerHTML = html;
        })
        .catch(err => {
          console.error('appliances error', err);
        });
    }

    function sendTraining(action) {
      const payload = {action: action};
      if (action === 'start') {
        payload.name = applianceNameEl.value;
      }
      fetch('/api/appliance/train', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      })
        .then(async r => {
          const data = await r.json();
          if (!r.ok) {
            trainingStatusEl.textContent = 'Error: ' + (data.error || 'unknown');
          }
          loadAppliances();
        })
        .catch(err => {
          console.error('train error', err);
        });
    }

    function loadInsights() {
      fetch('/api/insights')
        .then(r => r.json())
        .then(data => {
          const insights = data.insights || [];
          const anomalies = data.anomalies || [];
          let html = '';

          if (insights.length === 0 && anomalies.length === 0) {
            html = '<div>No recent insights or alerts.</div>';
          } else {
            if (insights.length > 0) {
              html += '<div class="card-title">Insights</div>';
              insights.forEach(i => {
                const savings = i.potential_savings != null
                  ? ' Save $' + (i.potential_savings || 0).toFixed(2)
                  : '';
                html += '<div style="margin-bottom:6px;">' +
                  i.message + savings +
                  '</div>';
              });
            }
            if (anomalies.length > 0) {
              html += '<div class="card-title" style="margin-top:8px;">Alerts</div>';
              anomalies.forEach(a => {
                html += '<div style="margin-bottom:6px;">' +
                  '<strong>[' + (a.severity || 'info') + ']</strong> ' +
                  (a.type || '') + ': ' + a.message +
                  '</div>';
              });
            }
          }

          insightsContainerEl.innerHTML = html;
        })
        .catch(err => {
          console.error('insights error', err);
        });
    }

    function loadAdvancedStats() {
      fetch('/api/advanced_stats')
        .then(r => r.json())
        .then(data => {
          const goal = data.goal;
          const footprint = data.footprint;
          const forecast = data.forecast;
          const vacation = data.vacation;
          const peak = data.peak;

          if (goal && goal.goal_kwh > 0) {
            const txt = 'Used ' + goal.used_kwh.toFixed(1) + ' / ' +
              goal.goal_kwh.toFixed(1) + ' kWh, projected ' +
              goal.projected_kwh.toFixed(1) + ' kWh, ' +
              (goal.on_track ? 'on track' : 'over goal');
            goalSummaryEl.textContent = txt;
          } else {
            goalSummaryEl.textContent = 'No goal set.';
          }

          if (footprint) {
            const txt = footprint.kwh.toFixed(1) + ' kWh, ' +
              footprint.co2_kg.toFixed(1) + ' kg CO2, ' +
              footprint.trees_equivalent.toFixed(1) + ' trees, ' +
              footprint.km_driven_equivalent.toFixed(0) + ' km in a car';
            co2SummaryEl.textContent = txt;
          } else {
            co2SummaryEl.textContent = 'No data yet.';
          }

          if (forecast) {
            const txt = forecast.day + ': about $' +
              forecast.predicted_cost.toFixed(2) +
              ' (' + (forecast.confidence || 'medium') + ' confidence)';
            forecastSummaryEl.textContent = txt;
          } else {
            forecastSummaryEl.textContent = 'No forecast yet.';
          }

          if (vacation && vacation.likely_away) {
            vacationSummaryEl.textContent = vacation.message;
          } else {
            vacationSummaryEl.textContent = 'Usage looks normal.';
          }

          if (peak && peak.warning) {
            peakSummaryEl.textContent = peak.message;
          } else {
            peakSummaryEl.textContent = 'No peak warning.';
          }
        })
        .catch(err => {
          console.error('advanced stats error', err);
        });
    }

    function loadApplianceCosts() {
      fetch('/api/appliance_costs?days=30')
        .then(r => r.json())
        .then(data => {
          const apps = data.appliances || [];
          if (apps.length === 0) {
            applianceCostChart.data.labels = [];
            applianceCostChart.data.datasets[0].data = [];
            applianceCostChart.update();
            applianceCostSubtitleEl.textContent = 'No appliance events logged yet.';
            return;
          }
          const labels = apps.map(a => a.name);
          const costs = apps.map(a => a.cost || 0);
          applianceCostChart.data.labels = labels;
          applianceCostChart.data.datasets[0].data = costs;
          applianceCostChart.update();

          const total = costs.reduce((sum, v) => sum + v, 0);
          applianceCostSubtitleEl.textContent =
            'Top ' + apps.length + ' appliances, total $' + total.toFixed(2) +
            ' in last ' + (data.days || 30) + ' days.';
        })
        .catch(err => {
          console.error('appliance cost error', err);
        });
    }

    function loadDiscovery() {
      fetch('/api/discovery')
        .then(r => r.json())
        .then(data => {
          const active = data.active;
          const startTs = data.start_ts;
          const suggestions = data.suggestions || [];

          if (active) {
            if (startTs) {
              const dt = new Date(startTs * 1000);
              discoveryStatusEl.textContent =
                'Discovery active since ' + dt.toLocaleString();
            } else {
              discoveryStatusEl.textContent = 'Discovery active.';
            }
          } else {
            discoveryStatusEl.textContent = 'Discovery inactive.';
          }

          discoveryListEl.innerHTML = '';
          if (suggestions.length === 0) {
            if (!active) {
              const li = document.createElement('li');
              li.textContent = 'No suggestions yet. Run discovery while you use appliances.';
              discoveryListEl.appendChild(li);
            }
          } else {
            suggestions.forEach(s => {
              const li = document.createElement('li');
              li.textContent =
                s.description + ' Â· ' +
                s.occurrences + ' runs Â· avg $' +
                s.avg_cost_per_use.toFixed(2) +
                ' Â· guess: ' + s.suggested_name;
              discoveryListEl.appendChild(li);
            });
          }
        })
        .catch(err => {
          console.error('discovery error', err);
        });
    }

    function setDiscovery(action) {
      fetch('/api/discovery', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action})
      })
        .then(r => r.json())
        .then(() => {
          loadDiscovery();
        })
        .catch(err => {
          console.error('set discovery error', err);
        });
    }

    dayPicker.addEventListener('change', () => {
      const val = dayPicker.value;
      loadHistoryForDay(val);
    });

    exportBtn.addEventListener('click', () => {
      const val = dayPicker.value;
      const url = val ? ('/api/export_csv?day=' + val) : '/api/export_csv';
      window.open(url, '_blank');
    });

    saveRatesBtn.addEventListener('click', saveRates);
    saveBudgetBtn.addEventListener('click', saveBudget);
    presetTouBtn.addEventListener('click', applyOntarioTouPreset);
    startTrainingBtn.addEventListener('click', () => sendTraining('start'));
    stopTrainingBtn.addEventListener('click', () => sendTraining('stop'));
    startDiscoveryBtn.addEventListener('click', () => setDiscovery('start'));
    stopDiscoveryBtn.addEventListener('click', () => setDiscovery('stop'));

    document.addEventListener('DOMContentLoaded', () => {
      const today = new Date();
      const iso = today.toISOString().slice(0, 10);
      dayPicker.value = iso;
      loadHistoryForDay(iso);
      loadMonthlyOverview();
      loadWeekOverview();
      loadPrediction();
      loadRates();
      loadSettings();
      loadAppliances();
      loadInsights();
      loadAdvancedStats();
      loadApplianceCosts();
      loadDiscovery();
      refreshLive();
      setInterval(refreshLive, 1000);
      setInterval(loadAppliances, 30000);
      setInterval(loadMonthlyOverview, 5 * 60 * 1000);
      setInterval(loadWeekOverview, 5 * 60 * 1000);
      setInterval(loadPrediction, 10 * 60 * 1000);
      setInterval(loadInsights, 5 * 60 * 1000);
      setInterval(loadAdvancedStats, 10 * 60 * 1000);
      setInterval(loadApplianceCosts, 15 * 60 * 1000);
      setInterval(loadDiscovery, 10 * 60 * 1000);
    });
  </script>
</body>
</html>
"""


# ----------------- Main -----------------

def main():
    global enhanced_detector
    init_db()
    enhanced_detector = ApplianceDetector(DB_PATH)

    t = threading.Thread(target=poll_sensor_loop, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()


