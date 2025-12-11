#!/usr/bin/env python3
import os
import sqlite3
import threading
import time
import json
import io
import csv
from collections import deque, defaultdict
from datetime import datetime, timedelta, date

import requests
from flask import Flask, jsonify, request, Response

# ------------- Config -------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "neurio_power.sqlite3")

SENSOR_URL = "http://192.168.50.165/current-sample"

POLL_INTERVAL_SECONDS = 1          # sample every second
AGGREGATE_INTERVAL_SECONDS = 120   # aggregate every 2 minutes
LIVE_WINDOW_SECONDS = 15 * 60      # keep 15 minutes of live data in RAM
RETENTION_DAYS = 365               # keep 12 months of history

DAILY_BUDGET_DEFAULT = 5.00        # default daily budget in dollars

# Default TOU rates in cents per kWh (storage)
DEFAULT_RATES = {
    "winter": {
        "off_peak": 9.8,
        "mid_peak": 15.7,
        "on_peak": 20.3,
    },
    "summer": {
        "off_peak": 9.8,
        "mid_peak": 15.7,
        "on_peak": 20.3,
    },
}

app = Flask(__name__)

# ------------- In-memory state -------------

recent_samples = deque(maxlen=LIVE_WINDOW_SECONDS)  # (ts, power_w)
agg_buffer = []
last_agg_ts = None

# TOU rates cache: (season, tou_period) -> rate_cents
RATES = {}

# Settings cache
DAILY_BUDGET = DAILY_BUDGET_DEFAULT

# Appliance learning / detection state
training_active = False
training_name = None
training_samples = []  # list of (ts, power_w)
appliance_signatures_cache = []  # list of dicts

current_event_name = None
current_event_start_ts = None
current_event_sum_delta = 0.0
current_event_count = 0
current_event_sig_avg = 0.0

state_lock = threading.Lock()

# Anomaly detection state
anomaly_state = {
    "recent_events": deque(maxlen=100),
    "last_check": 0,
    "alerts": [],
}

# Auto-discovery state
discovery_state = {
    "active": False,
    "start_ts": None,
    "discovered": [],
}

# ------------- Database helpers -------------

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_conn():
    return get_db_connection()


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # Main samples table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER PRIMARY KEY,
            power_w REAL NOT NULL,
            energy_kwh REAL NOT NULL,
            cost REAL NOT NULL,
            season TEXT NOT NULL,
            tou_period TEXT NOT NULL,
            rate_cents REAL NOT NULL
        )
        """
    )

    # Rates table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rates (
            season TEXT NOT NULL,
            tou_period TEXT NOT NULL,
            rate_cents REAL NOT NULL,
            PRIMARY KEY (season, tou_period)
        )
        """
    )

    # Appliance signatures
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS appliance_signatures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            baseline_w REAL NOT NULL,
            avg_w REAL NOT NULL,
            peak_w REAL NOT NULL,
            created_ts INTEGER NOT NULL
        )
        """
    )

    # Appliance events
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS appliance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appliance_name TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            avg_w REAL NOT NULL,
            energy_kwh REAL NOT NULL,
            cost REAL NOT NULL,
            confidence REAL NOT NULL
        )
        """
    )

    # Settings
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

    # Smart insights
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

    # Seed default rates if missing (store in cents)
    for season, per in DEFAULT_RATES.items():
        for period, rate in per.items():
            cur.execute(
                """
                INSERT OR IGNORE INTO rates (season, tou_period, rate_cents)
                VALUES (?, ?, ?)
                """,
                (season, period, rate),
            )

    # Seed default daily budget if missing
    cur.execute(
        """
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('daily_budget', ?)
        """,
        (str(DAILY_BUDGET_DEFAULT),),
    )

    conn.commit()
    conn.close()

    load_rates()
    load_settings()
    load_appliance_signatures()


def load_rates():
    global RATES
    RATES = {}
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT season, tou_period, rate_cents FROM rates")
    for row in cur.fetchall():
        key = (row["season"], row["tou_period"])
        RATES[key] = float(row["rate_cents"])
    conn.close()

    for season, per in DEFAULT_RATES.items():
        for period, rate in per.items():
            key = (season, period)
            if key not in RATES:
                RATES[key] = rate


def update_rate(season, tou_period, rate_cents):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO rates (season, tou_period, rate_cents)
        VALUES (?, ?, ?)
        ON CONFLICT(season, tou_period)
        DO UPDATE SET rate_cents = excluded.rate_cents
        """,
        (season, tou_period, rate_cents),
    )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return default
    return row["value"]


def set_setting(key, value):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def load_settings():
    global DAILY_BUDGET
    val = get_setting("daily_budget", str(DAILY_BUDGET_DEFAULT))
    try:
        DAILY_BUDGET = float(val)
    except Exception:
        DAILY_BUDGET = DAILY_BUDGET_DEFAULT


def load_appliance_signatures():
    global appliance_signatures_cache
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name, baseline_w, avg_w, peak_w, created_ts
        FROM appliance_signatures
        ORDER BY created_ts
        """
    )
    rows = cur.fetchall()
    conn.close()
    appliance_signatures_cache = [
        {
            "name": r["name"],
            "baseline_w": float(r["baseline_w"]),
            "avg_w": float(r["avg_w"]),
            "peak_w": float(r["peak_w"]),
            "created_ts": int(r["created_ts"]),
        }
        for r in rows
    ]


def prune_old_data():
    cutoff_ts = int(time.time()) - RETENTION_DAYS * 86400
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM samples WHERE ts < ?", (cutoff_ts,))
    conn.commit()
    conn.close()

# ------------- TOU classification -------------

def classify_tou(dt_local):
    month = dt_local.month
    weekday = dt_local.weekday()
    hour = dt_local.hour

    if month in (11, 12, 1, 2, 3):
        season = "winter"
    else:
        season = "summer"

    is_weekend = weekday >= 5
    if is_weekend:
        tou_period = "off_peak"
    else:
        if season == "winter":
            if 7 <= hour < 11 or 17 <= hour < 19:
                tou_period = "on_peak"
            elif 11 <= hour < 17:
                tou_period = "mid_peak"
            else:
                tou_period = "off_peak"
        else:
            if 11 <= hour < 17:
                tou_period = "on_peak"
            elif 7 <= hour < 11 or 17 <= hour < 19:
                tou_period = "mid_peak"
            else:
                tou_period = "off_peak"

    key = (season, tou_period)
    rate_cents = RATES.get(key, DEFAULT_RATES[season][tou_period])
    return season, tou_period, rate_cents


def get_tou_info(ts):
    dt_local = datetime.fromtimestamp(ts)
    season, tou_period, rate_cents = classify_tou(dt_local)
    return season, tou_period, rate_cents / 100.0


def store_aggregate(ts, avg_power_w):
    if avg_power_w is None:
        return

    interval_seconds = AGGREGATE_INTERVAL_SECONDS
    energy_kwh = avg_power_w * interval_seconds / 1000.0 / 3600.0

    dt_local = datetime.fromtimestamp(ts)
    season, tou_period, rate_cents = classify_tou(dt_local)
    cost = energy_kwh * rate_cents / 100.0

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO samples
            (ts, power_w, energy_kwh, cost, season, tou_period, rate_cents)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, avg_power_w, energy_kwh, cost, season, tou_period, rate_cents),
    )
    conn.commit()
    conn.close()

    prune_old_data()

# ------------- Sensor polling -------------

def read_neurio_power_w():
    try:
        resp = requests.get(SENSOR_URL, timeout=2)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    power_w = None
    channels = data.get("channels", [])
    for ch in channels:
        if ch.get("type") == "CONSUMPTION":
            try:
                power_w = float(ch.get("p_W", 0.0))
            except Exception:
                power_w = 0.0
            break

    if power_w is None and channels:
        try:
            power_w = sum(float(ch.get("p_W", 0.0)) for ch in channels)
        except Exception:
            power_w = 0.0

    return power_w


def estimate_baseline_from_recent_locked():
    powers = [p for _, p in recent_samples if p is not None]
    if not powers:
        return 0.0
    powers_sorted = sorted(powers)
    idx = max(0, int(len(powers_sorted) * 0.1) - 1)
    return powers_sorted[idx]


def store_appliance_event_locked(name, start_ts, end_ts,
                                 avg_delta_w, sig_avg_w):
    if end_ts <= start_ts:
        return

    duration_seconds = (end_ts - start_ts) + POLL_INTERVAL_SECONDS
    energy_kwh = avg_delta_w * duration_seconds / 1000.0 / 3600.0

    mid_ts = (start_ts + end_ts) // 2
    dt_mid = datetime.fromtimestamp(mid_ts)
    season, tou_period, rate_cents = classify_tou(dt_mid)
    cost = energy_kwh * rate_cents / 100.0

    if sig_avg_w > 0:
        confidence = max(
            0.0,
            1.0 - abs(avg_delta_w - sig_avg_w) / sig_avg_w
        )
    else:
        confidence = 0.0

    anomaly_state["recent_events"].append(
        {
            "appliance": name,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "avg_w": avg_delta_w,
            "energy_kwh": energy_kwh,
            "cost": cost,
        }
    )

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO appliance_events
            (appliance_name, start_ts, end_ts, avg_w, energy_kwh, cost, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, start_ts, end_ts, avg_delta_w, energy_kwh, cost, confidence),
    )

    # TOU optimization insight
    savings = calculate_tou_savings(name, avg_delta_w, duration_seconds)
    if savings:
        try:
            cur.execute(
                """
                INSERT INTO insights
                    (ts, type, message, potential_savings, created_ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    "tou_optimization",
                    (
                        f"Running {savings['appliance']} now costs ${cost:.2f}. "
                        f"Wait {savings['hours_until_off_peak']}h for off-peak to save "
                        f"${savings['savings']:.2f}"
                    ),
                    savings["savings"],
                    time.time(),
                ),
            )
        except Exception:
            pass

    conn.commit()
    conn.close()


def close_current_event_locked(ts_now):
    global current_event_name, current_event_start_ts
    global current_event_sum_delta, current_event_count, current_event_sig_avg

    if current_event_name is None or current_event_count <= 0:
        current_event_name = None
        current_event_start_ts = None
        current_event_sum_delta = 0.0
        current_event_count = 0
        current_event_sig_avg = 0.0
        return

    min_samples = max(30 // POLL_INTERVAL_SECONDS, 5)
    if current_event_count >= min_samples:
        avg_delta = current_event_sum_delta / current_event_count
        store_appliance_event_locked(
            current_event_name,
            current_event_start_ts,
            ts_now,
            avg_delta,
            current_event_sig_avg,
        )

    current_event_name = None
    current_event_start_ts = None
    current_event_sum_delta = 0.0
    current_event_count = 0
    current_event_sig_avg = 0.0


def handle_training_locked(ts_now, power_w):
    global training_samples
    if training_active and power_w is not None:
        training_samples.append((ts_now, power_w))


def handle_detection_locked(ts_now, power_w):
    global current_event_name, current_event_start_ts
    global current_event_sum_delta, current_event_count, current_event_sig_avg

    if not appliance_signatures_cache or power_w is None:
        close_current_event_locked(ts_now)
        return

    baseline = estimate_baseline_from_recent_locked()
    delta = max(0.0, power_w - baseline)

    best_name = None
    best_sig_avg = 0.0
    best_score = 0.0

    for sig in appliance_signatures_cache:
        sig_delta = sig["avg_w"]
        if sig_delta <= 0:
            continue
        diff = abs(delta - sig_delta)
        threshold = max(0.3 * sig_delta, 200.0)
        if diff > threshold:
            continue
        score = 1.0 - diff / threshold
        if score > best_score:
            best_score = score
            best_name = sig["name"]
            best_sig_avg = sig_delta

    matched_name = best_name

    if matched_name is None:
        close_current_event_locked(ts_now)
        return

    if current_event_name is None:
        current_event_name = matched_name
        current_event_start_ts = ts_now
        current_event_sum_delta = delta
        current_event_count = 1
        current_event_sig_avg = best_sig_avg
    elif current_event_name == matched_name:
        current_event_sum_delta += delta
        current_event_count += 1
    else:
        close_current_event_locked(ts_now)
        current_event_name = matched_name
        current_event_start_ts = ts_now
        current_event_sum_delta = delta
        current_event_count = 1
        current_event_sig_avg = best_sig_avg


def finalize_appliance_training_locked():
    global training_active, training_name, training_samples

    if not training_active or not training_samples:
        return {"error": "No training samples"}

    powers = [p for _, p in training_samples if p is not None]
    if not powers:
        training_active = False
        training_name = None
        training_samples = []
        return {"error": "No valid power samples"}

    baseline = min(powers)
    deltas = [max(0.0, p - baseline) for p in powers]
    if not deltas:
        training_active = False
        training_name = None
        training_samples = []
        return {"error": "No positive delta samples"}

    avg_delta = sum(deltas) / len(deltas)
    peak_delta = max(deltas)
    created_ts = int(training_samples[0][0])
    name = training_name or "unnamed"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO appliance_signatures
            (name, baseline_w, avg_w, peak_w, created_ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, baseline, avg_delta, peak_delta, created_ts),
    )
    conn.commit()
    conn.close()

    load_appliance_signatures()

    training_active = False
    training_name = None
    training_samples = []

    return {
        "ok": True,
        "name": name,
        "baseline_w": baseline,
        "avg_delta_w": avg_delta,
        "peak_delta_w": peak_delta,
    }

# ------------- Anomalies -------------

def detect_anomalies(conn=None):
    now_ts = time.time()
    if now_ts - anomaly_state["last_check"] < 300:
        return

    anomaly_state["last_check"] = now_ts
    alerts = []

    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    own_conn = False
    if conn is None:
        conn = get_conn()
        own_conn = True

    try:
        conn.row_factory = sqlite3.Row

        today_start = datetime(today.year, today.month, today.day).timestamp()
        cur = conn.execute(
            "SELECT AVG(power_w) AS avg FROM samples WHERE ts>=? AND ts<? "
            "AND CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER) "
            "BETWEEN 1 AND 5",
            (today_start, now_ts),
        )
        row = cur.fetchone()
        today_baseline = row["avg"] if row and row["avg"] is not None else None

        week_start = datetime(week_ago.year, week_ago.month, week_ago.day).timestamp()
        yesterday_end = datetime(
            yesterday.year, yesterday.month, yesterday.day
        ).timestamp()
        cur = conn.execute(
            "SELECT AVG(power_w) AS avg FROM samples WHERE ts>=? AND ts<? "
            "AND CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER) "
            "BETWEEN 1 AND 5",
            (week_start, yesterday_end),
        )
        row = cur.fetchone()
        week_baseline = row["avg"] if row and row["avg"] is not None else None

        if (
            today_baseline is not None
            and week_baseline is not None
            and today_baseline > week_baseline * 1.3
        ):
            alert = {
                "type": "high_baseline",
                "message": (
                    f"Always-on power is {int(today_baseline - week_baseline)}W higher than usual. "
                    f"Check for devices left on."
                ),
                "severity": "warning",
            }
            alerts.append(alert)

        for alert in alerts:
            try:
                conn.execute(
                    """
                    INSERT INTO anomaly_alerts
                        (ts, type, message, severity, acknowledged, created_ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (now_ts, alert["type"], alert["message"], alert["severity"], 0, now_ts),
                )
            except Exception:
                pass

        if alerts:
            conn.commit()

        anomaly_state["alerts"] = alerts
    finally:
        if own_conn:
            conn.close()

# ------------- Poll loop -------------

def poll_sensor_loop():
    global last_agg_ts, agg_buffer

    while True:
        ts_now = int(time.time())
        power_w = read_neurio_power_w()

        with state_lock:
            recent_samples.append((ts_now, power_w))

            handle_training_locked(ts_now, power_w)
            handle_detection_locked(ts_now, power_w)

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

        try:
            detect_anomalies()
        except Exception:
            pass

        time.sleep(POLL_INTERVAL_SECONDS)

# ------------- Analytics helpers -------------

def get_day_bounds(day_str):
    if day_str:
        dt_day = datetime.strptime(day_str, "%Y-%m-%d").date()
    else:
        dt_day = date.today()
    start_dt = datetime(dt_day.year, dt_day.month, dt_day.day)
    end_dt = start_dt + timedelta(days=1)
    return dt_day, int(start_dt.timestamp()), int(end_dt.timestamp())


def compute_daily_summary(rows, day_start_ts):
    if not rows:
        return {
            "day_str": datetime.fromtimestamp(day_start_ts).strftime("%Y-%m-%d"),
            "total_kwh": 0.0,
            "total_cost": 0.0,
            "max_kw": 0.0,
            "min_w": 0.0,
            "always_on_w": 0.0,
            "avg_kw": 0.0,
            "load_factor": 0.0,
        }

    powers = [float(r["power_w"]) for r in rows if r["power_w"] is not None]
    energies = [float(r["energy_kwh"]) for r in rows]
    costs = [float(r["cost"]) for r in rows]

    total_kwh = sum(energies)
    total_cost = sum(costs)

    if powers:
        min_w = min(powers)
        max_w = max(powers)
        sorted_p = sorted(powers)
        idx = max(0, int(len(sorted_p) * 0.1) - 1)
        always_on_w = sorted_p[idx]
    else:
        min_w = 0.0
        max_w = 0.0
        always_on_w = 0.0

    avg_kw = total_kwh / 24.0 if total_kwh > 0 else 0.0
    max_kw = max_w / 1000.0 if max_w > 0 else 0.0
    load_factor = (avg_kw / max_kw) if max_kw > 0 else 0.0

    return {
        "day_str": datetime.fromtimestamp(day_start_ts).strftime("%Y-%m-%d"),
        "total_kwh": total_kwh,
        "total_cost": total_cost,
        "max_kw": max_kw,
        "min_w": min_w,
        "always_on_w": always_on_w,
        "avg_kw": avg_kw,
        "load_factor": load_factor,
    }


def get_daily_summary_for_date(dt_day):
    start_dt = datetime(dt_day.year, dt_day.month, dt_day.day)
    end_dt = start_dt + timedelta(days=1)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts, power_w, energy_kwh, cost, season, tou_period, rate_cents
        FROM samples
        WHERE ts >= ? AND ts < ?
        ORDER BY ts
        """,
        (start_ts, end_ts),
    )
    rows = cur.fetchall()
    conn.close()
    return compute_daily_summary(rows, start_ts)


def query_range_totals(start_dt, end_dt):
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(energy_kwh) AS kwh, SUM(cost) AS cost FROM samples WHERE ts >= ? AND ts < ?",
        (start_ts, end_ts),
    )
    row = cur.fetchone()
    conn.close()
    kwh = row[0] if row and row[0] is not None else 0.0
    cost = row[1] if row and row[1] is not None else 0.0
    return kwh, cost


def predict_monthly_cost():
    today = date.today()
    first_this = date(today.year, today.month, 1)
    start_ts = datetime(first_this.year, first_this.month, 1).timestamp()
    now_ts = time.time()

    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT SUM(cost) AS total FROM samples WHERE ts>=? AND ts<?",
            (start_ts, now_ts),
        )
        row = cur.fetchone()
        spent_so_far = row["total"] if row and row["total"] is not None else 0.0
    finally:
        conn.close()

    days_elapsed = (today - first_this).days + 1
    if days_elapsed == 0:
        return {"predicted_monthly": 0, "spent_so_far": 0, "confidence": "low"}

    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    days_in_month = (next_month - first_this).days

    daily_avg = spent_so_far / days_elapsed
    predicted = daily_avg * days_in_month

    if days_elapsed > 7:
        confidence = "high"
    elif days_elapsed > 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "predicted_monthly": round(predicted, 2),
        "spent_so_far": round(spent_so_far, 2),
        "daily_average": round(daily_avg, 2),
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "confidence": confidence,
    }


def calculate_tou_savings(appliance_name, avg_w, duration_s):
    now_ts = time.time()
    _, current_period, current_rate = get_tou_info(now_ts)

    dt = datetime.fromtimestamp(now_ts)
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
    off_peak_cost = energy_kwh * off_peak_rate
    savings = current_cost - off_peak_cost

    if savings < 0.10:
        return None

    return {
        "appliance": appliance_name,
        "savings": round(savings, 2),
        "hours_until_off_peak": hours_until,
        "current_period": current_period,
    }

# 1. Appliance Cost Breakdown

def get_appliance_costs_breakdown(days=30):
    since = time.time() - (days * 24 * 3600)
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT appliance_name, SUM(cost) AS total_cost, "
            "SUM(energy_kwh) AS total_kwh, COUNT(*) AS runs "
            "FROM appliance_events WHERE start_ts>=? "
            "GROUP BY appliance_name ORDER BY total_cost DESC",
            (since,),
        )
        return [
            {
                "name": r["appliance_name"],
                "cost": r["total_cost"],
                "kwh": r["total_kwh"],
                "runs": r["runs"],
            }
            for r in cur
        ]
    finally:
        conn.close()

# 2. Weather Correlation placeholder

def get_weather_correlation():
    return None

# 3. Appliance Health Monitoring

def detect_appliance_degradation(appliance_id):
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT energy_kwh, start_ts FROM appliance_events "
            "WHERE appliance_name=? AND start_ts>=? ORDER BY start_ts",
            (appliance_id, time.time() - 30 * 24 * 3600),
        )
        events = [{"kwh": r["energy_kwh"], "ts": r["start_ts"]} for r in cur]

        if len(events) < 5:
            return None

        mid = len(events) // 2
        first_half_avg = sum(e["kwh"] for e in events[:mid]) / mid
        second_half_avg = sum(e["kwh"] for e in events[mid:]) / (len(events) - mid)

        if second_half_avg > first_half_avg * 1.2:
            increase_pct = ((second_half_avg - first_half_avg) / first_half_avg) * 100
            return {
                "appliance_id": appliance_id,
                "increase_pct": increase_pct,
                "message": (
                    f"Appliance using {int(increase_pct)} percent more energy than before"
                ),
            }
        return None
    finally:
        conn.close()

# 4. Goal Tracking

def track_energy_goal():
    goal_kwh = float(get_setting("goal_monthly_kwh", "0"))
    if goal_kwh == 0:
        return None

    today = date.today()
    month_start = date(today.year, today.month, 1).timestamp()

    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT SUM(energy_kwh) AS used FROM samples WHERE ts>=?",
            (month_start,),
        )
        row = cur.fetchone()
        used = row["used"] or 0 if row and row["used"] is not None else 0

        days_elapsed = (today - date(today.year, today.month, 1)).days + 1
        if today.month == 12:
            days_in_month = 31
        else:
            days_in_month = (
                date(today.year, today.month + 1, 1)
                - date(today.year, today.month, 1)
            ).days

        projected = (used / days_elapsed) * days_in_month if days_elapsed > 0 else used
        pct_to_goal = (used / goal_kwh) * 100 if goal_kwh > 0 else 0

        return {
            "goal_kwh": goal_kwh,
            "used_kwh": round(used, 1),
            "projected_kwh": round(projected, 1),
            "pct_to_goal": round(pct_to_goal, 1),
            "on_track": projected <= goal_kwh,
        }
    finally:
        conn.close()

# 5. Peak Demand Predictor

def predict_peak_demand():
    with state_lock:
        if not recent_samples:
            return None
        _, current_w = recent_samples[-1]
        if current_w is None:
            return None

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT MAX(power_w) AS max_today FROM samples WHERE ts>=?",
            (today_start,),
        )
        row = cur.fetchone()
        max_today = row["max_today"] or 0 if row and row["max_today"] is not None else 0

        if current_w > max_today * 0.9 and max_today > 0:
            return {
                "warning": True,
                "current_w": current_w,
                "todays_peak": max_today,
                "message": (
                    f"You're at {int(current_w)}W, close to today's peak of {int(max_today)}W!"
                ),
            }
        return None
    finally:
        conn.close()

# 6. Vacation Mode Detection

def detect_vacation_mode():
    recent = time.time() - (3 * 24 * 3600)
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT AVG(power_w) AS avg FROM samples WHERE ts>=?",
            (recent,),
        )
        row = cur.fetchone()
        recent_avg = row["avg"] or 0 if row and row["avg"] is not None else 0

        month_ago = time.time() - (30 * 24 * 3600)
        cur = conn.execute(
            "SELECT AVG(power_w) AS avg FROM samples "
            "WHERE ts>=? AND ts<? "
            "AND CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INTEGER) "
            "BETWEEN 1 AND 5",
            (month_ago, recent),
        )
        row = cur.fetchone()
        typical_baseline = row["avg"] or 0 if row and row["avg"] is not None else 0

        if typical_baseline > 0 and recent_avg < typical_baseline * 1.2:
            return {
                "likely_away": True,
                "current_avg": int(recent_avg),
                "message": "Usage pattern suggests you're away. Baseline power looks normal.",
            }
        return None
    finally:
        conn.close()

# 7. Bill Simulator

def simulate_different_rate_plan(plan_rates):
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

    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT energy_kwh, season, tou_period FROM samples WHERE ts>=? AND ts<?",
            (start_ts, end_ts),
        )

        actual_cost = 0.0
        simulated_cost = 0.0

        for row in cur:
            kwh = row["energy_kwh"] or 0
            season = row["season"]
            period = row["tou_period"]

            actual_rate_cents = RATES.get((season, period), DEFAULT_RATES[season][period])
            actual_rate = actual_rate_cents / 100.0
            actual_cost += kwh * actual_rate

            sim_rate = plan_rates.get(season, {}).get(period, actual_rate)
            simulated_cost += kwh * sim_rate

        difference = simulated_cost - actual_cost
        savings_pct = (
            ((actual_cost - simulated_cost) / actual_cost * 100.0)
            if actual_cost > 0
            else 0.0
        )

        return {
            "actual_cost": round(actual_cost, 2),
            "simulated_cost": round(simulated_cost, 2),
            "difference": round(difference, 2),
            "savings_pct": round(savings_pct, 1),
        }
    finally:
        conn.close()

# 8. Carbon Footprint Tracker

def calculate_carbon_footprint():
    ONTARIO_CO2_PER_KWH = 0.040

    today = date.today()
    month_start = date(today.year, today.month, 1).timestamp()

    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT SUM(energy_kwh) AS total FROM samples WHERE ts>=?",
            (month_start,),
        )
        row = cur.fetchone()
        kwh = row["total"] or 0 if row and row["total"] is not None else 0

        co2_kg = kwh * ONTARIO_CO2_PER_KWH

        trees_to_offset = co2_kg / 21.0
        km_driven = co2_kg / 0.192

        return {
            "kwh": round(kwh, 1),
            "co2_kg": round(co2_kg, 1),
            "trees_equivalent": round(trees_to_offset, 1),
            "km_driven_equivalent": round(km_driven, 0),
        }
    finally:
        conn.close()

# 9. Smart Load Forecasting

def forecast_tomorrow():
    tomorrow = date.today() + timedelta(days=1)

    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        costs = []
        for weeks_ago in range(1, 5):
            target_date = tomorrow - timedelta(weeks=weeks_ago)
            start = datetime(
                target_date.year, target_date.month, target_date.day
            ).timestamp()
            end = start + 24 * 3600

            cur = conn.execute(
                "SELECT SUM(cost) AS total FROM samples WHERE ts>=? AND ts<?",
                (start, end),
            )
            row = cur.fetchone()
            cost = row["total"] or 0 if row and row["total"] is not None else 0
            if cost > 0:
                costs.append(cost)

        if not costs:
            return None

        avg_cost = sum(costs) / len(costs)

        return {
            "predicted_cost": round(avg_cost, 2),
            "day": tomorrow.strftime("%A"),
            "confidence": "high" if len(costs) >= 3 else "medium",
        }
    finally:
        conn.close()

# 10. Appliance Auto-Discovery

def run_auto_discovery_analysis():
    if not discovery_state["active"]:
        return []

    start_ts = discovery_state["start_ts"]
    now_ts = time.time()

    if not start_ts or now_ts - start_ts < 3600:
        return []

    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT start_ts, end_ts, energy_kwh, cost FROM appliance_events WHERE start_ts>=?",
            (start_ts,),
        )

        events = []
        for row in cur:
            duration = row["end_ts"] - row["start_ts"]
            events.append(
                {
                    "start": row["start_ts"],
                    "duration_min": int(duration / 60),
                    "kwh": row["energy_kwh"],
                    "cost": row["cost"],
                }
            )

        clusters = defaultdict(list)
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

        discovery_state["discovered"] = suggestions
        return suggestions
    finally:
        conn.close()


def guess_appliance_name(duration_min, kwh):
    if duration_min <= 0:
        avg_w = 0
    else:
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

# 11. Real-time Notifications

def send_notification(title, message, severity="info"):
    webhook_url = get_setting("webhook_url")
    if not webhook_url:
        return

    try:
        requests.post(
            webhook_url,
            json={
                "title": title,
                "message": message,
                "severity": severity,
            },
            timeout=5,
        )
    except Exception:
        pass

# 12. Voice Summary

def get_voice_summary():
    pred = predict_monthly_cost()
    return (
        f"Your predicted bill this month is ${pred['predicted_monthly']}. "
        f"You're currently using {pred['daily_average']} dollars per day. "
        f"You've spent {pred['spent_so_far']} dollars so far this month."
    )

# ------------- API routes -------------

@app.route("/")
def index():
    return DASHBOARD_HTML


@app.route("/api/live")
def api_live():
    with state_lock:
        if recent_samples:
            ts, power_w = recent_samples[-1]
        else:
            ts = int(time.time())
            power_w = None

    error = None
    if power_w is None:
        error = "No recent sample"
        power_w_val = 0.0
    else:
        power_w_val = float(power_w)

    dt_local = datetime.fromtimestamp(ts)
    season, tou_period, rate_cents = classify_tou(dt_local)
    rate_per_kwh = rate_cents / 100.0
    cost_per_hour = (power_w_val / 1000.0) * rate_per_kwh

    return jsonify(
        {
            "error": error,
            "power_w": power_w_val,
            "rate_cents": rate_cents,
            "rate_per_kwh": rate_per_kwh,
            "season": season,
            "tou_period": tou_period,
            "ts": float(ts),
            "cost_per_hour": cost_per_hour,
        }
    )


@app.route("/api/history")
def api_history():
    day_str = request.args.get("day")
    dt_day, start_ts, end_ts = get_day_bounds(day_str)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts, power_w, energy_kwh, cost, season, tou_period, rate_cents
        FROM samples
        WHERE ts >= ? AND ts < ?
        ORDER BY ts
        """,
        (start_ts, end_ts),
    )
    rows = cur.fetchall()
    conn.close()

    samples = []
    totals_energy = 0.0
    totals_cost = 0.0

    per_period_map = {}
    hourly_map = {}

    for r in rows:
        ts = int(r["ts"])
        power_w = float(r["power_w"])
        energy_kwh = float(r["energy_kwh"])
        cost = float(r["cost"])
        season = r["season"]
        tou_period = r["tou_period"]
        rate_cents = float(r["rate_cents"])

        totals_energy += energy_kwh
        totals_cost += cost

        samples.append(
            {
                "power_w": power_w,
                "ts": float(ts),
                "ts_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        key = (season, tou_period, rate_cents)
        if key not in per_period_map:
            per_period_map[key] = {"energy_kwh": 0.0, "cost": 0.0}
        per_period_map[key]["energy_kwh"] += energy_kwh
        per_period_map[key]["cost"] += cost

        hour_dt = datetime.fromtimestamp(ts).replace(
            minute=0, second=0, microsecond=0
        )
        hour_ts = int(hour_dt.timestamp())
        if hour_ts not in hourly_map:
            hourly_map[hour_ts] = {
                "sum_power_w": 0.0,
                "count": 0,
                "energy_kwh": 0.0,
                "cost": 0.0,
            }
        hourly_map[hour_ts]["sum_power_w"] += power_w
        hourly_map[hour_ts]["count"] += 1
        hourly_map[hour_ts]["energy_kwh"] += energy_kwh
        hourly_map[hour_ts]["cost"] += cost

    per_period = []
    for (season, tou_period, rate_cents), agg in per_period_map.items():
        per_period.append(
            {
                "season": season,
                "tou_period": tou_period,
                "rate_cents": rate_cents,
                "energy_kwh": agg["energy_kwh"],
                "cost": agg["cost"],
            }
        )

    hourly = []
    for hour_ts, agg in sorted(hourly_map.items()):
        count = agg["count"]
        avg_power_w = agg["sum_power_w"] / count if count > 0 else 0.0
        hourly.append(
            {
                "ts_hour": float(hour_ts),
                "ts_hour_str": datetime.fromtimestamp(hour_ts).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "avg_power_w": avg_power_w,
                "energy_kwh": agg["energy_kwh"],
                "cost": agg["cost"],
            }
        )

    totals = {"energy_kwh": totals_energy, "cost": totals_cost}
    daily_summary = compute_daily_summary(rows, start_ts)

    budget = DAILY_BUDGET
    budget_hit_ts = None
    if budget and budget > 0:
        running = 0.0
        for r in rows:
            running += float(r["cost"])
            if running >= budget:
                budget_hit_ts = int(r["ts"])
                break

    if budget_hit_ts is not None:
        budget_hit_str = datetime.fromtimestamp(budget_hit_ts).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    else:
        budget_hit_str = None

    budget_info = {
        "budget": float(budget),
        "hit_ts": budget_hit_ts,
        "hit_str": budget_hit_str,
        "spent": totals_cost,
        "remaining": max(0.0, budget - totals_cost),
    }

    return jsonify(
        {
            "hourly": hourly,
            "per_period": per_period,
            "samples": samples,
            "totals": totals,
            "daily_summary": daily_summary,
            "budget_info": budget_info,
        }
    )


@app.route("/api/monthly_overview")
def api_monthly_overview():
    now = datetime.now()
    this_start = datetime(now.year, now.month, 1)
    this_end = now

    if now.month == 1:
        last_start = datetime(now.year - 1, 12, 1)
    else:
        last_start = datetime(now.year, now.month - 1, 1)
    last_end = this_start

    this_kwh, this_cost = query_range_totals(this_start, this_end)
    last_kwh, last_cost = query_range_totals(last_start, last_end)

    delta_kwh = this_kwh - last_kwh
    delta_cost = this_cost - last_cost

    pct_kwh = (delta_kwh / last_kwh * 100.0) if last_kwh > 0 else None
    pct_cost = (delta_cost / last_cost * 100.0) if last_cost > 0 else None

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)

    try:
        y_summary = get_daily_summary_for_date(yesterday)
        y_always = y_summary.get("always_on_w", 0.0)
    except Exception:
        y_always = 0.0

    try:
        p_summary = get_daily_summary_for_date(day_before)
        p_always = p_summary.get("always_on_w", 0.0)
    except Exception:
        p_always = 0.0

    delta_always = y_always - p_always

    return jsonify(
        {
            "this_month": {"kwh": this_kwh, "cost": this_cost},
            "last_month": {"kwh": last_kwh, "cost": last_cost},
            "delta_kwh": delta_kwh,
            "delta_cost": delta_cost,
            "pct_kwh": pct_kwh,
            "pct_cost": pct_cost,
            "always_on": {
                "yesterday_w": y_always,
                "prev_w": p_always,
                "delta_w": delta_always,
            },
        }
    )


@app.route("/api/week_overview")
def api_week_overview():
    now = datetime.now()
    today = now.date()
    weekday = today.weekday()
    this_start_date = today - timedelta(days=weekday)
    this_start = datetime(
        this_start_date.year, this_start_date.month, this_start_date.day
    )
    this_end = now

    last_start = this_start - timedelta(days=7)
    last_end = this_start

    this_kwh, this_cost = query_range_totals(this_start, this_end)
    last_kwh, last_cost = query_range_totals(last_start, last_end)

    delta_kwh = this_kwh - last_kwh
    delta_cost = this_cost - last_cost

    pct_kwh = (delta_kwh / last_kwh * 100.0) if last_kwh > 0 else None
    pct_cost = (delta_cost / last_cost * 100.0) if last_cost > 0 else None

    return jsonify(
        {
            "this_week": {"kwh": this_kwh, "cost": this_cost},
            "last_week": {"kwh": last_kwh, "cost": last_cost},
            "delta_kwh": delta_kwh,
            "delta_cost": delta_cost,
            "pct_kwh": pct_kwh,
            "pct_cost": pct_cost,
            "week_start_str": this_start.strftime("%Y-%m-%d"),
        }
    )


@app.route("/api/prediction")
def api_prediction():
    try:
        data = predict_monthly_cost()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/insights")
def api_insights():
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row

        cur = conn.execute(
            "SELECT * FROM insights WHERE dismissed=0 ORDER BY ts DESC LIMIT 10"
        )
        insights_list = []
        for row in cur:
            insights_list.append(
                {
                    "message": row["message"],
                    "potential_savings": row["potential_savings"],
                }
            )

        cur = conn.execute(
            "SELECT * FROM anomaly_alerts WHERE acknowledged=0 "
            "ORDER BY ts DESC LIMIT 5"
        )
        anomalies = []
        for row in cur:
            anomalies.append(
                {
                    "type": row["type"],
                    "message": row["message"],
                    "severity": row["severity"],
                }
            )

        return jsonify({"insights": insights_list, "anomalies": anomalies})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/appliance_costs")
def api_appliance_costs():
    days = request.args.get("days", default=30, type=int)
    try:
        items = get_appliance_costs_breakdown(days)
        return jsonify({"days": days, "appliances": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/advanced_stats")
def api_advanced_stats():
    goal = track_energy_goal()
    footprint = calculate_carbon_footprint()
    forecast = forecast_tomorrow()
    vacation = detect_vacation_mode()
    peak = predict_peak_demand()
    return jsonify(
        {
            "goal": goal,
            "footprint": footprint,
            "forecast": forecast,
            "vacation": vacation,
            "peak": peak,
        }
    )

@app.route("/api/discovery", methods=["GET", "POST"])
def api_discovery():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        now_ts = time.time()
        if action == "start":
            discovery_state["active"] = True
            discovery_state["start_ts"] = now_ts
            discovery_state["discovered"] = []
            return jsonify(
                {
                    "status": "started",
                    "active": True,
                    "start_ts": now_ts,
                }
            )
        elif action == "stop":
            discovery_state["active"] = False
            suggestions = run_auto_discovery_analysis()
            return jsonify(
                {
                    "status": "stopped",
                    "active": False,
                    "suggestions": suggestions,
                }
            )
        else:
            return jsonify({"error": "Unknown action"}), 400

    suggestions = run_auto_discovery_analysis()
    return jsonify(
        {
            "active": discovery_state["active"],
            "start_ts": discovery_state["start_ts"],
            "suggestions": suggestions,
        }
    )

@app.route("/api/export_csv")
def api_export_csv():
    day_str = request.args.get("day")
    dt_day, start_ts, end_ts = get_day_bounds(day_str)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts, power_w, energy_kwh, cost, season, tou_period, rate_cents
        FROM samples
        WHERE ts >= ? AND ts < ?
        ORDER BY ts
        """,
        (start_ts, end_ts),
    )
    rows = cur.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "timestamp",
            "local_time",
            "power_w",
            "energy_kwh",
            "cost",
            "season",
            "tou_period",
            "rate_cents",
        ]
    )

    for r in rows:
        ts = int(r["ts"])
        writer.writerow(
            [
                ts,
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                float(r["power_w"]),
                float(r["energy_kwh"]),
                float(r["cost"]),
                r["season"],
                r["tou_period"],
                float(r["rate_cents"]),
            ]
        )

    csv_data = output.getvalue()
    output.close()

    filename = f"power_{dt_day.isoformat()}.csv"
    resp = Response(csv_data, mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/api/rates", methods=["GET", "POST"])
def api_rates():
    if request.method == "GET":
        nested = {"winter": {}, "summer": {}}
        for season in ("winter", "summer"):
            for period in ("off_peak", "mid_peak", "on_peak"):
                key = (season, period)
                cents = RATES.get(key, DEFAULT_RATES[season][period])
                nested[season][period] = cents / 100.0
        return jsonify({"rates": nested})

    data = request.get_json(silent=True) or {}
    rates = data.get("rates", {})

    for season in ("winter", "summer"):
        s_obj = rates.get(season, {})
        for period in ("off_peak", "mid_peak", "on_peak"):
            if period in s_obj:
                try:
                    dollars = float(s_obj[period])
                except (TypeError, ValueError):
                    continue
                cents = dollars * 100.0
                update_rate(season, period, cents)

    load_rates()
    return jsonify({"status": "ok"})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(
            {
                "daily_budget": DAILY_BUDGET,
            }
        )

    data = request.get_json(silent=True) or {}
    if "daily_budget" in data:
        try:
            val = float(data["daily_budget"])
        except (TypeError, ValueError):
            return jsonify({"error": "invalid daily_budget"}), 400
        set_setting("daily_budget", str(val))
        load_settings()

    return jsonify(
        {
            "status": "ok",
            "daily_budget": DAILY_BUDGET,
        }
    )


@app.route("/api/appliances", methods=["GET"])
def api_appliances():
    now_ts = int(time.time())
    cutoff = now_ts - 86400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT appliance_name, start_ts, end_ts, avg_w, energy_kwh, cost, confidence
        FROM appliance_events
        WHERE start_ts >= ?
        ORDER BY start_ts DESC
        LIMIT 50
        """,
        (cutoff,),
    )
    rows = cur.fetchall()
    conn.close()

    with state_lock:
        training = {
            "active": training_active,
            "name": training_name,
        }
        signatures = list(appliance_signatures_cache)

    events = []
    for r in rows:
        start_ts = int(r["start_ts"])
        end_ts = int(r["end_ts"])
        events.append(
            {
                "appliance_name": r["appliance_name"],
                "start_ts": start_ts,
                "end_ts": end_ts,
                "start_str": datetime.fromtimestamp(start_ts).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "end_str": datetime.fromtimestamp(end_ts).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "avg_w": float(r["avg_w"]),
                "energy_kwh": float(r["energy_kwh"]),
                "cost": float(r["cost"]),
                "confidence": float(r["confidence"]),
            }
        )

    return jsonify(
        {
            "training": training,
            "signatures": signatures,
            "events": events,
        }
    )


@app.route("/api/appliance/train", methods=["POST"])
def api_appliance_train():
    data = request.get_json(silent=True) or {}
    action = data.get("action")

    if action == "start":
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name is required"}), 400
        with state_lock:
            global training_active, training_name, training_samples
            training_active = True
            training_name = name
            training_samples = []
        return jsonify({"status": "ok", "mode": "training", "name": name})

    if action == "stop":
        with state_lock:
            result = finalize_appliance_training_locked()
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)

    return jsonify({"error": "Unknown action"}), 400

# ------------- Dashboard HTML -------------

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
          <span id="dayBudget">–</span>
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
            dayBudgetEl.textContent = '–';
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
          const text = (deltaW >= 0 ? '+' : '−') + Math.abs(deltaW).toFixed(0) + ' W vs prior night';
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
                sig.name + ' · Δavg ' +
                (sig.avg_w || 0).toFixed(0) + ' W · Δpeak ' +
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
                '<strong>' + ev.appliance_name + '</strong> · ' +
                ev.start_str + ' → ' + ev.end_str +
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
                s.description + ' · ' +
                s.occurrences + ' runs · avg $' +
                s.avg_cost_per_use.toFixed(2) +
                ' · guess: ' + s.suggested_name;
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

# ------------- Main -------------

def main():
    init_db()
    t = threading.Thread(target=poll_sensor_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
