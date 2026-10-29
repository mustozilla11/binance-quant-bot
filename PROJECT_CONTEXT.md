# PROJECT_CONTEXT.md
> **Target Audience:** AI Coding Agents & LLMs  
> **Purpose:** Ultra-dense, token-efficient system context. Ingest this single file instead of loading raw codebase files or transcript history.

---

## 1. System Identity & Mission
* **Name:** Binance Quant Paper Trading Bot (USDT-M Futures).
* **Mode:** Simulated Execution (Paper Trading) with institutional-grade quantitative filters, zero API keys required for read-only public endpoints.
* **Stack:** Python 3.12, Pandas, NumPy, Requests, Threading.
* **Persistence:** `paper_trades.csv` (Trade Ledger), `active_positions.json` (State Recovery across restarts), `config.json` (Hot-reloadable parameters).
* **Notifications & Control:** Two-way Telegram bot integration (broadcast alerts + interactive command polling).

---

## 2. File Map & Architectural Responsibilities

| File | Primary Responsibility | Key Functions / Classes |
| :--- | :--- | :--- |
| [`bot.py`](file:///home/mustafao/.gemini/antigravity/scratch/binance_quant_bot/bot.py) | Main trading orchestrator, position lifecycle, trailing stop, MFE/MAE tracking, Telegram command handler. | `PaperTradingBot`, `Position` dataclass, `scan_market()`, `update_and_check_positions()` |
| [`market_structure.py`](file:///home/mustafao/.gemini/antigravity/scratch/binance_quant_bot/market_structure.py) | Quant analytics: Fractal S/R clustering, Volume Profile (POC), Wilder RSI, HTF (1H) EMA trend, ATR-based dynamic TP/SL. | `analyze_market_structure()`, `find_sr_clusters()`, `calculate_volume_profile_poc()`, `calculate_atr()`, `calculate_rsi()` |
| [`binance_client.py`](file:///home/mustafao/.gemini/antigravity/scratch/binance_quant_bot/binance_client.py) | REST API client for Binance Futures endpoints with CloudFront backup failover & in-memory TTL caching. | `BinanceFuturesClient`, `get_top_volume_pairs()`, `get_open_interest_change()`, `get_top_trader_long_short_ratio()`, `get_funding_rate()`, `get_24h_change_pct()` |
| [`optimizer.py`](file:///home/mustafao/.gemini/antigravity/scratch/binance_quant_bot/optimizer.py) | Walk-Forward Self-Optimization engine using train/test splits (70/30) & Grid Search on 14-day historical 15m klines. | `run_optimization()`, `evaluate_params_on_data()`, updates `config.json` |
| [`telegram_notifier.py`](file:///home/mustafao/.gemini/antigravity/scratch/binance_quant_bot/telegram_notifier.py) | Background async Telegram worker, message queuing, markup formatting, interactive command loop. | `TelegramNotifier`, `start_polling()`, `send_message()` |
| [`config.json`](file:///home/mustafao/.gemini/antigravity/scratch/binance_quant_bot/config.json) | Production hyperparameters (hot-reloaded by bot and modified by optimizer). | Volume thresholds, TP/SL rules, blacklist, cooldown, Telegram credentials |
| [`paper_trades.csv`](file:///home/mustafao/.gemini/antigravity/scratch/binance_quant_bot/paper_trades.csv) | Full audit ledger of all closed trades. Columns: `timestamp,symbol,side,entry_price,exit_price,qty,amount_usdt,gross_pnl_pct,pnl_pct,pnl_usd,commission_usd,duration_minutes,exit_reason,mfe_pct,mae_pct` |

---

## 3. Signal Generation & Screening Pipeline (5m Scan Loop)

1. **BTC Market Regime Circuit Breaker:**
   * Fetches `BTCUSDT` 15m return & 5m EMA(20). If 15m drop $\le -0.80\%$ or price is below EMA20, halts all altcoin scanning to prevent market-wide liquidation cascades.
2. **Volume Anomaly Screening:**
   * Filters top 25 USDT-M pairs (excludes blacklist, stablecoins, non-crypto/TradFi symbols).
   * 5m volume must be $\ge 2.8\times$ 20-period SMA volume.
3. **Directional Conflict Guard:**
   * If $\ge 4$ SHORT positions are open, blocks new LONG entries unless a confirmed Short Squeeze is identified (`funding_rate <= -0.01%`).
4. **Parabolic Pump Protection (TRB Trap Guard):**
   * Checks `/fapi/v1/ticker/24hr`. If 24h gain $\ge +60.0\%$, rejects LONG (exhaustion phase).
5. **Taker Volume Delta (Aggressive Flow):**
   * Taker Buy Base Volume / Total Volume must be $\ge 58\%$ for LONG (or Taker Sell $\ge 58\%$ for SHORT). Rejects passive limit book traps.
6. **Candle Wick / Pinbar Filter:**
   * Close strength must be $\ge 65\%$ of candle range: `(Close - Low) / (High - Low) >= 0.65`. Rejects shooting stars / fakeouts.
7. **Institutional Open Interest (OI) Confirmation:**
   * 15-minute OI must increase by $\ge +2.0\%$ confirming fresh smart money positioning, not short-covering.
8. **Top Trader Long/Short Ratio:**
   * For LONG: ratio must be $\le 2.20$ (prevents entering crowded retail FOMO longs).
9. **Market Structure & Volatility Sanity:**
   * **Fractal S/R Clusters:** Minimum 1.2% room to nearest resistance (room-to-run).
   * **Wilder RSI(14):** Must be $\le 78.0$ (not overbought).
   * **1H HTF Trend:** Confirms Higher-Timeframe EMA alignment.
   * **Volatility Cap:** 5m ATR% must be $\le 4.5\%$ (protects against blown-out spreads and liquidity gaps).

---

## 4. Position Sizing, Volatility Adaptation & Exits

### A. Mathematical Volatility Sizing (ATR, Not Hardcoded Coin Names)
* The bot **never** hardcodes coin names or static market cap tiers. It uses relative volatility:  
  $$\text{atr\_pct} = \left(\frac{\text{ATR}_{14}}{\text{Price}}\right) \times 100$$
* **Dynamic Take-Profit (TP):** $\text{suggested\_tp} = \text{clip}(\text{atr\_pct} \times 2.5, 1.8\%, 6.0\%)$. Clamped $0.15\%$ below nearest resistance.
* **Dynamic Stop-Loss (SL):** $\text{suggested\_sl} = \text{clip}(\text{atr\_pct} \times 2.0, 1.0\%, 3.5\%)$. Tucked $0.15\%$ below nearest swing support.
* Low-beta coins (BNB, BTC) get tight targets ($\sim 1.8\%$ TP, $\sim 1.2\%$ SL); high-beta meme coins (PEPE, DOGE) automatically get wider targets ($\sim 4.5\%$ TP, $\sim 3.0\%$ SL).

### B. In-Trade Management
* **Break-Even Lock:** Once unrealized gain hits $+1.50\%$, SL moves to $\text{entry} + 0.20\%$ (risk-free trade).
* **Trailing Stop:** Activates at TP level, trails with $0.6\%$ delta from peak.
* **Time Expiration:** Automatically closes stagnating trades after $40\text{ min}$ (LONG) or $15\text{ min}$ (SHORT).
* **Excursion Tracking:** Records real-time **MFE** (Maximum Favorable Excursion) and **MAE** (Maximum Adverse Excursion) for post-trade analytics.
* **Slippage & Commission Accounting:** Deducts fixed $0.12\%$ roundtrip fee per trade in `paper_trades.csv`.

---

## 5. Telegram Interface & Admin Commands

* `/durum`: Real-time status of open positions, current prices, live PnL %, peak PnL, duration.
* `/rapor` or `/ozet`: Comprehensive performance summary (Win Rate, Net PnL USDT, Sanal Komisyon, Avg MFE, Avg MAE, Best/Worst trade).
* `/kapat <SEMBOL>`: Instantly closes a specific position at market price (e.g. `/kapat SOLUSDT`).
* `/kapat hepsi`: Emergency liquidation of all active positions.

---

## 6. Development & Extension Guidelines for Agents
* **Do NOT hardcode coin names:** Always derive behavior from volume, ATR, OI, funding rate, or orderbook delta.
* **Short Strategy Segregation:** If modifying short entry logic, consult user specifications first (user requested isolating short strategies).
* **Process Management:** The bot runs as a background process via `.venv/bin/python bot.py`. Use `pkill -f "python bot.py"` before restarting.
