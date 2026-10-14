MEXC FUTURES SIGNAL BOT — NATIVE 5m

Files: bot.py, requirements.txt, Procfile, railway.json, README_MEXC.txt

Railway Variables:
TELEGRAM_BOT_TOKEN=your Telegram bot token
TELEGRAM_CHAT_ID=your chat id (multiple IDs comma-separated)
MEXC_SYMBOLS=BTCUSDT,ETHUSDT
TEST_MODE=false
POLL_SECONDS=10 (optional)

MEXC source: public Contract K-line API, interval=Min5 (native 5-minute candles).
Symbols are converted to MEXC contract form BTC_USDT and ETH_USDT. No MEXC API key is required for public candle data.

Rules:
- 🟢🟢🟢🔴🔴🔴 => LONG
- 🔴🔴🔴🟢🟢🟢 => SHORT
- PRE-ALERT during the final minute of candle #6
- SIGNAL only after candle #6 closes
- WIN if expected color appears in candle #7, #8, #9, #10, #11, or #12
- LOSS only after candle #12 closes without a matching candle
- Alerts only; no orders are placed.

Deploy: upload all files to Railway. Start command: python bot.py. Add the variables above.
Offline test: set TEST_MODE=true and run python bot.py. This does not test live MEXC/Telegram connectivity.
