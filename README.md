# Neurio-Local-Power-Monitor
Self-hosted home energy monitor for Neurio / whole-home power meters, with a Flask + SQLite backend and a live web dashboard that does TOU billing, appliance detection, anomaly alerts, forecasting, and energy insights.
## Overview

This project is a self-hosted home energy monitoring and analytics system designed to run on a Raspberry Pi (or any Linux box) and talk to a Neurio (or similar) whole-home power meter.

It continuously polls the meter, stores data in a local SQLite database, and serves a web dashboard with rich analytics: live power, historical usage, appliance breakdown, TOU-aware billing, anomaly detection, and smart insights about how you use electricity.

### Key features

- **Live power dashboard**
  - Polls the Neurio endpoint every second for real-time power data.
  - Shows current whole-home wattage and short-term history.
  - Aggregates samples into time blocks for efficient storage and graphing.

- **Historical usage & TOU billing**
  - Stores data in an SQLite database (`neurio_power.db`).
  - Aggregates energy (kWh) and cost over minutes / hours / days.
  - Supports Ontario-style Time-Of-Use (TOU) rates for summer and winter.
  - Computes daily, weekly, and monthly energy and cost.

- **Appliance detection & event logging**
  - Detects on/off “events” based on power signatures.
  - Logs appliance runs to `appliance_events` with:
    - start / end timestamps  
    - average power, energy (kWh), and cost per run
  - Supports an appliance list so you can name and classify loads.
  - Includes an auto-discovery mode that clusters repeated patterns and suggests probable appliances.

- **Insights, anomalies, and TOU optimization**
  - **Anomaly detection** scans for unusual patterns, such as:
    - Elevated overnight “always-on” baseline compared to the last week.
  - **TOU optimization** suggests savings by shifting runs to off-peak hours.
  - Stores insights and anomaly alerts in dedicated tables:
    - `insights` for optimization tips and potential savings  
    - `anomaly_alerts` for warnings about abnormal usage

- **Predictions & goals**
  - **Monthly bill prediction**  
    - Estimates end-of-month cost based on current spend and days elapsed.
  - **Tomorrow forecast**  
    - Predicts tomorrow’s cost using same-day-of-week history.
  - **Energy goals**  
    - Optional monthly kWh goal (`goal_monthly_kwh` setting) with:
      - used kWh, projected kWh, percent to goal, on-track flag.

- **Appliance cost & health analytics**
  - **Cost breakdown**  
    - Computes which appliances cost the most over a configurable window (default 30 days).
  - **Degradation detection**  
    - Checks if a given appliance’s energy per run is trending up (possible failing motor, clogged filter, etc.).

- **Carbon footprint & “what-if” scenarios**
  - **Carbon footprint tracker**
    - Converts monthly kWh to CO₂ emissions using an Ontario grid factor.
    - Provides equivalents such as trees required to offset and km driven in a typical car.
  - **Rate-plan simulator**
    - Replays last month’s usage against an alternate TOU rate table and shows:
      - actual vs simulated bill  
      - absolute and percentage difference.

- **Vacation mode & peak alerts**
  - **Vacation detection**
    - Compares recent usage to historical nighttime baseline to identify “away” patterns and warn if usage is oddly high while you are likely gone.
  - **Peak demand predictor**
    - Watches current power versus today’s historical max and warns when you are close to setting a new daily peak (useful for demand-based billing).

- **Web API and dashboard**
  - Flask web server exposes JSON endpoints for:
    - live samples  
    - historical blocks and day profiles  
    - appliance events and summaries  
    - predictions, insights, anomalies, and goals.
  - Single-page HTML + JavaScript dashboard that:
    - auto-refreshes live data  
    - renders charts for 60-second, 24-hour, and calendar views  
    - shows insights, anomalies, goals, footprint, and forecast panels.

- **Notifications & voice summary**
  - Optional webhook integration to push alerts (Pushover, Discord, Telegram, etc.).
  - Simple “voice summary” text endpoint suitable for TTS:
    - summarizes predicted monthly bill, daily average, and spend so far.

### Tech stack

- **Backend:** Python, Flask, `requests`
- **Storage:** SQLite
- **Frontend:** HTML + vanilla JavaScript
- **Target device:** Raspberry Pi or any Linux host with Python 3
- **Data source:** Neurio (or compatible) local HTTP API for current power samples
