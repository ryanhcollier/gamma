# Dealer Gamma Positioning & 60-Day Historical Ledger

A minimalist, brutalist web application for cash-account options trading at market open, driven by **Dealer Gamma Exposure (GEX)**, **Zero-Gamma Flip levels**, **Call Walls**, and **Put Walls** across **SPY** and **SPX**.

---

## Architecture & Features

### 1. UI & Visual Specifications
- **Palette & Layout:** Pitch-black background (`#000000`), stark white typography (`#FFFFFF`), brutalist high-contrast design.
- **Header Controls (Top-Left):**
  - **Info/Math Icon (`ⓘ` / Key `I`):** Opens a slide-over modal detailing real-time metrics (Spot, Zero-Gamma Flip, Call Wall, Put Wall, Regime, Dealer Hedging Mechanics, and Cash Account Rules).
  - **Backtest/History Icon (`⟲` / Key `H`):** Opens the **60-Day Backtest Ledger** detailing historical pre-market execution accuracy.
  - **Asset Toggle (`SPY` / `SPX` / Key `S`):** Instantly switch between SPY ETF and S&P 500 Index dealer positioning.
  - **Force Refresh Button:** Real-time re-querying and gamma re-computation.
- **Main View (Default):**
  - Vertically centered, full-viewport layout (`h-screen`, `w-screen`).
  - Large brutalist typography (`text-3xl` to `text-6xl`) dictating exactly 1 to 2 concise sentences for cash accounts.
    - *Positive Gamma Example:* `"Market is in positive gamma ($7722.72 > $7698.98 flip). Wait for an opening dip toward VWAP, then buy 3-7 DTE calls targeting $7750."`
    - *Negative Gamma Example:* `"Market has breached negative gamma ($769.64 < $770.70 flip). Buy 1-2 DTE puts targeting the $767 Put Wall, or stay in cash."`

---

### 2. 60-Day Historical Verification Engine (`GET /api/history`)
- **Data Pipeline:**
  1. Gathers 60 completed trading sessions with prior-day ($T-1$) pre-market gamma parameters: Zero-Gamma Flip, Call Wall, Put Wall.
  2. Evaluates Day $T$ Open relative to the Flip Level:
     - **Long Call Setup (+Gamma, $Open > Flip$):** Low tests opening dip/support; High reaches $\ge 0.5 \times (CallWall - Open)$ before market close without breaching Put Wall $\to$ `WIN`.
     - **Long Put Setup (-Gamma, $Open \le Flip$):** Directional breakdown confirming Low at or below Put Wall (or $\ge 1.0\%$ decline from Open) without invalidation $\to$ `WIN`.
     - Breached stops or adverse reversals $\to$ `LOSS`.
     - Non-triggered or tight sideways consolidations $\to$ `SCRATCH`.
- **Metrics Calculated:**
  - Execution Win Rate (`%`)
  - Total Winning, Losing, and Scratch Sessions
  - Average Daily Return Proxy (`%`)
  - Regime Performance Breakdown (+Gamma vs -Gamma)

---

### 3. Backend Endpoints (`app.py`)
- `GET /api/action?symbol=SPY`
  - Returns current spot, flip level, call wall, put wall, net gamma, regime, and 1-2 sentence trade action.
- `GET /api/history?symbol=SPY`
  - Returns 60 sessions with pre-market levels, outcomes (`WIN` / `LOSS` / `SCRATCH`), return proxies, and trade notes.
- `GET /`
  - Serves single-page application.
---

### 4. Data Providers & Resilient Hierarchy
1. **ThetaData Nexus Cloud API (`thetadatadx-py`):**
   - Connects directly to ThetaData's distributed options & equity cloud endpoints using `THETADATA_API_KEY`.
   - Uses `stock_history_eod` (SPY) and `index_history_eod` (SPX) for institutional OHLCV market bars.
   - Vectorized Black-Scholes gamma calculation across active near-term expirations and strike distributions.
2. **yfinance Fallback:**
   - Automatically takes over if ThetaData is unreachable or free tier rate limits (20 req/min) are reached.
3. **Persistent Local Cache & Calibrated Engine (`cache/`):**
   - Guarantees instant sub-millisecond response times and flawless offline resilience.

---

## Hostinger Deployment & Database Setup (`reil.studio/gamma`)

### 1. API Key Security & Isolation
- **No Hardcoded Keys:** Your ThetaData API key lives strictly in `.env` on your private server.
- **Git Protection:** `.env` is ignored via [`.gitignore`](file:///Users/collier/gamma/.gitignore) and will never be pushed to version control.
- **Zero Frontend Exposure:** The browser and public network only ever request `/api/action` and `/api/history`. No API key, database credentials, or secret tokens are ever transmitted in API JSON responses or HTML.

### 2. Hostinger MySQL Database Configuration
1. In your **Hostinger hPanel**, go to **Databases** $\to$ **MySQL Databases**.
2. Create a new database (e.g. `u123456789_gamma`) and a database user with full privileges.
3. Configure your server's `.env` file (copied from [`.env.example`](file:///Users/collier/gamma/.env.example)):
   ```ini
   THETADATA_API_KEY=your_thetadata_api_key_here

   DB_ENGINE=mysql
   DB_HOST=localhost
   DB_PORT=3306
   DB_NAME=u123456789_gamma
   DB_USER=u123456789_gammauser
   DB_PASSWORD=your_database_password

   ROOT_PATH=/gamma
   ```
4. The system automatically initializes the `daily_gamma_ledger` table with primary key `(date, symbol)`. If database connection details are omitted, it smoothly falls back to an embedded SQLite database (`cache/gamma_ledger.db`).

### 3. Subpath Hosting at `reil.studio/gamma`
- Both root (`/`) and subpath (`/gamma`) are natively handled in `app.py` and `static/index.html`.
- If using **Nginx** reverse proxy on your Hostinger VPS, add:
  ```nginx
  location /gamma/ {
      proxy_pass http://127.0.0.1:8000/gamma/;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```

### 4. Automated Daily Cron Collection (Hostinger Cron Jobs)
To guarantee real-time pre-market execution and an uninterrupted historical ledger, set up **two cron jobs** in Hostinger **hPanel** $\to$ **Advanced** $\to$ **Cron Jobs**:

1. **Morning Run (10 Minutes Before Market Open):**
   - **Time:** 9:20 AM ET (13:20 UTC during EDT / 14:20 UTC during EST, Monday–Friday: `20 13 * * 1-5` or `20 14 * * 1-5`).
   - **What it does:** Fetches the pre-market options chain from ThetaData Nexus, calculates today's Spot, Zero-Gamma Flip, Call/Put Walls, and updates the live trade command on the dashboard (`action_SPY.json` / `action_SPX.json`).
   - **Command:**
     ```bash
     cd /home/u794438130/domains/reil.studio/public_html/gamma && python3 cron_collector.py
     ```

2. **Evening Run (Post-Market Session Close):**
   - **Time:** 4:15 PM ET (20:15 UTC during EDT / 21:15 UTC during EST, Monday–Friday: `15 20 * * 1-5` or `15 21 * * 1-5`).
   - **What it does:** Pulls the completed day's official High/Low/Close price bars, grades the morning setup (`WIN`, `LOSS`, or `SCRATCH`), updates win rates, and permanently logs the day into the 60-Day Historical Backtest Ledger (`history_SPY.json` / `history_SPX.json`).
   - **Command:**
     ```bash
     cd /home/u794438130/domains/reil.studio/public_html/gamma && python3 cron_collector.py
     ```

---

## Real-Time Gamma Alerting Engine (`alert_engine.py`)

A zero-cost automated entry & exit alerting state machine built around dealer gamma structure.

### 1. Three-Layer Decision Matrix
1. **Regime State (Net GEX):** Checks whether `Spot > Zero_Gamma_Flip` (+Gamma mean reversion) or `Spot < Zero_Gamma_Flip` (-Gamma momentum expansion).
2. **Location Filter:** Checks if Spot enters within $0.25–$0.50 of the Major Put Wall, Call Wall, or Volatility Bands.
3. **Execution Confirmation:** Evaluates 1-minute reversal candle wicks (hammer / wick rejection) and volume expansion ($RVOL \ge 1.5\times$ 20-period SMA). Replaces expensive tick-level CVD with zero accounts required.

### 2. Concrete Strategy Set
* **`ALERT_BUY_DIP` (+Gamma Long):** Spot tests within $0.50 of Put Wall between 10:00 AM and 2:30 PM ET, prints a lower-wick reversal candle, and closes back above the wall.
* **`ALERT_FADE_RIP` (+Gamma Short):** Spot tests within $0.50 of Call Wall, rejects with an upper wick, and closes below the wall.
* **`ALERT_BULL_EXPANSION` (-Gamma Long):** Spot breaks above consolidation high or crosses Zero-Flip with $1.5\times$ volume spike.
* **`ALERT_BEAR_EXPANSION` (-Gamma Short):** Spot breaks down below Put Wall with $1.5\times$ volume spike.

### 3. Structural Exit Rules
* **Target (TP1):** Reaches Gamma Wall / Pivot $\to$ Sell 50–70%, trail remainder.
* **Structural Invalidation (SL):** 1-minute candle close past Entry Strike $\pm \$0.40 \to$ Full immediate market exit.
* **Regime Flip Cut:** Spot crosses Flip Line $\to$ Flatten position (dealer edge neutralized).
* **Time Cutoff:** Current Time $\ge$ 3:15 PM ET $\to$ Flatten all intraday contracts before theta collapse.

### 4. Running the Alert Monitor Daemon
```bash
# Continuous 60-second real-time monitor
python3 alert_engine.py --symbol SPY --interval 60

# Single evaluation tick
python3 alert_engine.py --symbol SPY --once
```

### 5. Multi-Channel Dispatch Configuration
In your `.env` file (optional):
```ini
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=123456789
```
* **macOS Desktop Notifications:** If run on Mac, native system notifications display automatically via `osascript` with sound.
* **Web UI Live Alert Card:** Active alerts automatically appear in the brutalist web interface at `/gamma` or `reil.studio/gamma`.
* **REST API:** Query `GET /api/alerts?symbol=SPY` or evaluate live `POST /api/alerts/evaluate?symbol=SPY`.

---

## Running Locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment (creates local SQLite fallback if DB not set)
cp .env.example .env
# Edit .env with your THETADATA_API_KEY

# 3. Start the FastAPI server
uvicorn app:app --reload --port 8000

# 4. Open in browser
http://localhost:8000/gamma
```



