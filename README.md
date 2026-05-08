# BTC Microstructure Bot

ربات paper-trading برای Hyperliquid با دو استراتژی microstructure:
- **Liquidation Fade** — fade کردن cascade های لیکوئیداسیون (تمام coin های انتخابی)
- **Liquidity Wall Reversal** — معکوس از wall های واقعی order book (BTC تنها)

> ⚠️ **این یک ابزار research است، نه print pool.** هیچ تضمینی برای سوددهی نیست. قبل از live، باید ۲-۴ هفته paper اجرا شود.

---

## 0) چه چیزی در v0.2 (این نسخه) عوض شد

این بازنگری یک round جدی review را adress می‌کند:

**P0 (مهم‌ترین):**
- ✅ **Liquidation feed validation با explicit status**: `liq_feed_status` در DB، یکی از `unknown` / `validated` / `unavailable`. وقتی unavailable شود، strategy hard-blocked می‌شود تا operator بعد از بررسی raw_ws_samples اون را با `/api/liq_feed/override` clear کند.
- ✅ **`live_armed` force-reset on EVERY startup**: حتی اگر در DB قبلاً true بود (به‌خاطر crash بعد از arming)، init_db همیشه به false برمی‌گرداند. یک activity log ERROR ثبت می‌شود تا operator ببیند. هیچ راهی برای resume خودکار live بعد از restart نیست.
- ✅ **Raw WS logging** با `RAW_WS_LOG=true` و sampled persistence
- ✅ **Three-stage live gate**: `LIVE_CODE_ENABLED` + `ENABLE_LIVE` + DB `live_armed`
- ✅ **Frozen Config دیگر mutate نمی‌شود** — toggles در `BotState` هستند
- ✅ **Live exits hard guard**: `REQUIRE_PROTECTED_EXITS=true` (default)

**P1:**
- ✅ Reconciliation loop: هر دقیقه DB live_trades vs HL exchange state.
- ✅ Risk شامل live exposure: max_concurrent_positions حالا paper+live هر دو را شمارش می‌کند.
- ✅ Per-strategy / per-coin position limits.
- ✅ Auto-pause بعد از N consecutive losses (`MAX_CONSEC_LOSSES`).
- ✅ Per-coin staleness check (`COIN_STALE_S`).
- ✅ Wall events حالا persist می‌شوند با full lifecycle.
- ✅ Aggregated rejection stats در `/api/reject_stats`.
- ✅ Daily report endpoint با MAE/MFE/profit factor/breakdown.
- ✅ MAE/MFE tracking روی هر trade.

**P2:**
- ✅ Startup safety: refuse to start with default token + public host.
- ✅ Restricted CORS (env-driven).
- ✅ Stronger auth (constant-time compare، default token rejected).
- ✅ تست‌های live safety، stale coin، liquidation parser.

---

## 1) معماری

```
┌────────────────────────────────────────────────────────────┐
│                    Hyperliquid WebSocket                   │
│  trades (همه coins) + l2Book (BTC only)                    │
└──────────────────────┬─────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────┐
│  HyperliquidListener                                       │
│   - reconnect + heartbeat                                  │
│   - liquidation field detector (3 shapes tolerated)        │
│   - diagnostic counters (/api/feed_health)                 │
│   - sampled raw WS log (RAW_WS_LOG=true)                   │
│   ↓                                                        │
│  MarketState (in-memory: trades, L2, candles)              │
└──────────────────────┬─────────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Orchestrator                                            │
│   strategies → RiskManager → Paper + (Live, gated)       │
│   wall persistence loop (every 5s)                       │
│   reconciliation loop (every 60s)                        │
│   feed validation (after 1 hour)                         │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  TradeManager (every 250ms)                              │
│   - SL/TP/time stop                                      │
│   - MAE/MFE tracking                                     │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Postgres                                                │
│   bot_state (singleton: equity, toggles, live_armed)     │
│   paper_trades, live_trades (with MAE/MFE)               │
│   signals, signal_reject_stats (sampled)                 │
│   wall_events (full lifecycle), liquidation_events       │
│   activity_log, raw_ws_samples                           │
└──────────────────────┬───────────────────────────────────┘
                       ▼
                FastAPI :PORT  ←── React Dashboard
```

---

## 2) Live Trading Safety Model — مهم‌ترین بخش

ارسال یک live order به HL نیاز به **سه دروازه** دارد که همگی باید open باشند:

```
LIVE_CODE_ENABLED=true   →  SDK اصلاً import می‌شود
ENABLE_LIVE=true         →  Exchange client ساخته می‌شود (نیاز به keys)
live_armed=true (DB)     →  orders واقعاً می‌روند به exchange
```

**Fail-safe مهم:** `live_armed` در DB **روی هر startup به false force-reset می‌شود**.
این یعنی اگر ربات crash کند بعد از اینکه شما live arm کرده‌اید، با restart خودکار به حالت disarmed برمی‌گردد. هیچ راهی برای resume خودکار live بعد از restart نیست.
وقتی این اتفاق بیفتد، یک activity log ERROR ثبت می‌شود تا در dashboard ببینید.

**به علاوه per-order:**
- `size_usd <= LIVE_MAX_ORDER_USD` (default $500)
- slippage در ورود `<= LIVE_MAX_SLIP_BPS` (default 30bps)
- `REQUIRE_PROTECTED_EXITS=true` ⟹ live order rejected می‌شود تا exchange-side stops پیاده شوند

**پیامد:** در حالت پیش‌فرض همهٔ env vars، **غیرممکن است** که live order اتفاقی برود حتی اگر دکمهٔ "LIVE ARMED" را در dashboard بزنید — چون `REQUIRE_PROTECTED_EXITS=true` به‌صورت پیش‌فرض است و ما هنوز exchange-side stops را پیاده نکرده‌ایم.

---

## 2.5) Liquidation Feed Validation — مدل state صریح

استراتژی liquidation_fade کاملاً به این که Hyperliquid trades feed دارای فیلد `liquidation` باشد وابسته است. اگر این feed shape نداشته باشد یا change کرده باشد، استراتژی نباید silently اجرا شود.

**State machine (در `BotState.liq_feed_status`):**

```
unknown   → بدو شروع. Strategy اجازهٔ run دارد (validation در حال انجام).
                ↓ اولین liq trade دیده شد
validated → استراتژی به‌طور عادی اجرا می‌شود
                ↓ یا شروع از unknown، بعد از ۱ ساعت بدون liq:
unavailable → استراتژی HARD-BLOCKED. حتی اگر operator toggle را روشن کند
              یا کسی check_can_trade را مستقیم صدا بزند، رد می‌شود.
              نیاز به override صریح operator دارد.
```

**Fail-safe:** بر هر startup، `liq_feed_status` به `unknown` reset می‌شود. این یعنی:
- بعد از redeploy/crash، validation تازه شروع می‌شود
- اگر feed قبلاً validated بود ولی الان نیست (مثلاً HL تغییر داده)، در ۱ ساعت اول کشف می‌شود

**اگر feed unavailable شد:**
1. در dashboard pill قرمز "liq feed UNAVAILABLE" را می‌بینید
2. در پنل Strategies، یک بنر قرمز با راهنمایی برای بررسی `raw_ws_samples`
3. در DB:
   ```sql
   SELECT * FROM raw_ws_samples WHERE has_liquidation = TRUE LIMIT 5;
   SELECT channel, count(*) FROM raw_ws_samples GROUP BY channel;
   ```
4. اگر feed درست بود ولی parser مشکل داشت → `parsers.py` را fix کنید
5. اگر confirm کردید feed درست است → دکمهٔ "Override → validated" در dashboard را بزنید (یا `POST /api/liq_feed/override` با `{"status": "validated"}`)

**از `/api/toggle` با `force=true`** اگر می‌خواهید همزمان liq_fade را روشن و feed status را override کنید:
```bash
curl -X POST https://your-host/api/toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"strategy": "liq", "enabled": true, "force": true}'
```

---

## 3) راه‌اندازی روی Railway

### الف) Project setup
1. به [railway.app](https://railway.app) → New Project → Deploy from GitHub repo (یا Empty Project + Railway CLI)
2. Add **PostgreSQL** to project → Railway خودکار `DATABASE_URL` می‌سازد

### ب) DATABASE_URL prefix
Railway می‌دهد `postgresql://...` ولی این کد به async نیاز دارد. متغیر را به این تغییر دهید:
```
postgresql+asyncpg://...
```

### ج) Environment Variables (حداقل)
```
DATABASE_URL=postgresql+asyncpg://...   # از Railway با prefix asyncpg
API_TOKEN=<random 32+ char string>      # برای کنترل dashboard. هرگز default نگذارید
PORT=                                   # auto by Railway
LOG_LEVEL=INFO
```

**برای multi-week paper test، این env vars اضافه را پیشنهاد می‌کنم:**
```
RAW_WS_LOG=true             # هفته‌ٔ اول، برای validation فید
RAW_WS_SAMPLE_EVERY=200     # ۱ از هر ۲۰۰ trade ضبط می‌شود + همه‌ٔ liquidations
ENABLE_LIQ_FADE=true
ENABLE_WALL=true
ENABLE_LIVE=false           # هرگز in paper era تغییر ندهید
LIVE_CODE_ENABLED=false     # SDK اصلاً نباشد
COINS=BTC,ETH,SOL,HYPE      # کم بدارید برای feed را بیشتر signal
PAPER_BALANCE=1000
RISK_PCT=1.5
MAX_POS=3
MAX_POS_STRAT=2
MAX_POS_COIN=1
MAX_POS_USD=200
DAILY_LOSS_PCT=5
DD_CIRCUIT_PCT=20
MAX_CONSEC_LOSSES=3
COIN_STALE_S=60
```

### د) Deploy
Railway خودش `requirements.txt` را detect می‌کند و `python -m src.main` را از `Procfile` اجرا می‌کند.

### هـ) Public URL
Railway یک public URL می‌دهد. به آن بروید — dashboard را می‌بینید. token را paste کنید.

**⚠️ هشدار امنیتی:** اگر `API_TOKEN` در default ("change-me-please") بماند **و** `API_HOST=0.0.0.0` (default در Railway) باشد، main.py از start کردن **refuse** می‌کند. حتما token قوی ست کنید.

---

## 4) Multi-Week Paper Test Workflow

### هفتهٔ ۱: Feed Validation
1. در dashboard → **Feed** tab بروید
2. بعد از ۱۰-۲۰ دقیقه ببینید:
   - **Trades total** > 1000؟ (yes → feed سالم است)
   - **Liq-flagged**: حتی یکی هم؟ اگر صفر بعد از ساعت اول، خودکار liq_fade disable می‌شود
   - **Liq shape**: dict / bool / other؟ این به ما می‌گوید feed چه می‌فرستد
3. بعد از ۲۴ ساعت در `raw_ws_samples` table با query SQL ببینید:
   ```sql
   SELECT * FROM raw_ws_samples WHERE has_liquidation = TRUE LIMIT 5;
   ```
   اگر این جدول خالی است → feed liquidation ندارد و باید رویکرد جایگزین (مثلاً subscription به `userEvents`) امتحان شود.

### هفتهٔ ۲-۳: Signal Quality
1. **Rejects** tab — ببینید چرا signal ها fire نمی‌شوند:
   - "cooldown ()" زیاد → strategy تلاش زیاد می‌کند، threshold را بالاتر ببرید
   - "no liq in window" زیاد → طبیعی، یعنی liquidation ها rare هستند
   - "flow not flipped" زیاد → این یعنی signal early reject شده، شاید 0.55/0.45 خیلی سختگیر است
2. **Report** tab بعد از ۳۰+ trade:
   - Win rate
   - Profit factor (>1.5 خوب، <1.0 strategy کار نمی‌کند)
   - MAE: اگر زیاد بزرگ، stop خیلی دور است
   - MFE: اگر بسیار بزرگ‌تر از actual PnL، target زود بسته می‌شود

### هفتهٔ ۴: Decision
- Profit factor > 1.5 + Sharpe > 1.0 + 100+ trades → ممکن است edge باشد، live considered
- Profit factor 1.0-1.5 → tune thresholds و یک هفته دیگر paper
- Profit factor < 1.0 → strategy edge ندارد، بازنگری منطق لازم است

### Live Decision (بسیار محتاطانه)
1. exchange-side stops در `_place_protected_exits` پیاده کنید
2. Hyperliquid testnet برای ۱ هفته
3. آنگاه mainnet با `LIVE_MAX_ORDER_USD=50`
4. ۲ هفته live با کم size
5. اگر edge ادامه داشت، size را تدریجی بالا ببرید

---

## 5) ساختار پروژه

```
btc-bot/
├── src/
│   ├── main.py                    # entry: orch + uvicorn + safety checks
│   ├── config.py                  # frozen Config + validation
│   ├── orchestrator.py            # main loop + reconciliation + feed validation
│   ├── ws/
│   │   ├── hl_listener.py         # WS با reconnect + diagnostics
│   │   ├── parsers.py             # pure liquidation field parser (test-friendly)
│   │   └── market_state.py        # in-memory buffers
│   ├── strategies/
│   │   ├── liquidation_fade.py
│   │   ├── liquidity_wall.py
│   │   └── math_utils.py
│   ├── execution/
│   │   ├── paper_executor.py      # با MAE/MFE + consec_losses
│   │   ├── live_executor.py       # 3-stage gate + protected exits guard
│   │   └── manager.py             # SL/TP + MAE/MFE tracking
│   ├── risk/
│   │   ├── limits.py              # per-strategy/coin/consec/stale checks
│   │   └── sizing.py
│   ├── db/
│   │   ├── models.py              # +SignalRejectStat, RawWsSample, MAE/MFE
│   │   └── session.py             # +record_reject_stat, record_raw_ws_sample
│   └── api/
│       └── server.py              # daily report + feed_health + reject_stats
├── frontend/
│   ├── index.html
│   ├── app.jsx                    # tabs: overview/trades/feed/report/rejects/walls/activity
│   └── style.css
├── tests/
│   ├── test_strategies.py         # core strategy logic
│   ├── test_liquidation_parser.py # parser shape tolerance
│   ├── test_live_safety.py        # live gates
│   └── test_stale_coin.py         # per-coin staleness
├── requirements.txt
├── Procfile
├── railway.json
├── pytest.ini
├── .env.example
├── .gitignore
└── README.md
```

---

## 6) API Endpoints

**Public (read-only):**
```
GET  /                       → dashboard
GET  /health                 → simple ping
GET  /api/status             → equity, runtime toggles, ws health
GET  /api/feed_health        → diagnostic counters, per-coin freshness
GET  /api/activity           → activity feed
GET  /api/trades             → paper + live trades (with MAE/MFE)
GET  /api/signals            → recent signals (accepted + rejected)
GET  /api/reject_stats       → why nothing fired (aggregated)
GET  /api/walls              → live wall tracker
GET  /api/report/daily?days=N → comprehensive metrics
```

**Authenticated (Bearer token):**
```
POST /api/toggle             → {strategy: "liq|wall|live_armed", enabled: bool}
POST /api/pause / resume     → manual pause; resume also resets consec_losses
POST /api/clear_pause        → clear pause flag without resuming strategies
POST /api/emergency_stop     → close all + disarm live + pause
```

---

## 7) Local Development

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL=postgresql+asyncpg://postgres:dev@localhost:5432/postgres
export API_TOKEN=dev-token-123
export API_HOST=127.0.0.1   # local-only، تا safety guard ناراحت نشود
export PORT=8000
export RAW_WS_LOG=true

python -m src.main
```

به `http://127.0.0.1:8000` بروید.

### اجرای tests
```bash
pip install pytest
PYTHONPATH=. pytest tests/ -v
```

---

## 8) عیب‌یابی

**Feed health: 0 liq trades بعد از یک ساعت**
- Hyperliquid ممکن است liquidation field را در public trades feed نگذارد
- `raw_ws_samples` table را check کنید: شکل پیام چیست؟
- ممکن است نیاز به subscribe به `userEvents` باشد (نیاز به wallet)
- در این بازه liq_fade خودکار disabled می‌شود

**Dashboard خالی است**
- token را paste کنید (برای read endpoints لازم نیست)
- DevTools → Network → ببینید fetch موفق است؟

**هیچ signal فعال نمی‌شود حتی پس از روزها**
- این طبیعی است
- **Rejects** tab را ببینید — می‌گوید چرا
- اگر "no liq in window" زیاد است: feed مشکل دارد یا روز quiet است
- اگر "cascade below threshold" زیاد است: thresholds را پایین بیاورید

**Daily loss limit زده شد**
- خودکار pause می‌شود
- روز جدید UTC reset می‌شود؛ یا manual resume

**`Bot starts but immediately refuses`**
- لاگ را ببینید: probably `API_TOKEN` default است + `API_HOST=0.0.0.0`
- token قوی ست کنید

---

## 9) Disclaimer

این کد research است. هیچ تضمین سودآوری ندارد. مسئولیت هر loss با خود شماست.
- ۲-۴ هفته paper test با Sharpe > 1.0 و profit factor > 1.5 minimum
- exchange-side stops قبل از live ضروری است
- شروع live با size بسیار کم
