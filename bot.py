import json
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]
TIMEFRAME_SECONDS = 300
TIMEFRAME_LABEL = "5m"
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT"
POLL_SECONDS = max(5, int(os.getenv("POLL_SECONDS", "10")))
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
API_URL = os.getenv("MEXC_FUTURES_KLINE_URL", "https://api.mexc.com/api/v1/contract/kline/{symbol}").strip()
# MEXC retired contract.mexc.com for Futures API access in January 2026.
# Normalize a legacy Railway variable too, so it cannot silently keep the retired host.
API_URL = API_URL.replace("https://contract.mexc.com", "https://api.mexc.com")


def normalize_symbol(value):
    s = value.strip().upper().replace("_", "")
    if not s:
        return ""
    return f"{s[:-4]}_USDT" if s.endswith("USDT") else f"{s}_USDT"

SYMBOLS = list(dict.fromkeys(normalize_symbol(s) for s in os.getenv("MEXC_SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()))
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("mexc-futures-signal-bot-5m")
SESSION = requests.Session()
SESSION.headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "MEXC-Futures-5m-Signal-Bot/7.0"})


def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M")


def symbol_label(symbol):
    return symbol.replace("_", "")


def candle_color(candle):
    if candle["close"] > candle["open"]: return "GREEN"
    if candle["close"] < candle["open"]: return "RED"
    return "DOJI"


def send_telegram(text):
    if TEST_MODE:
        print("\n--- TEST TELEGRAM ---\n" + text + "\n---------------------")
        return True
    if not TOKEN or not CHAT_IDS:
        log.error("TELEGRAM NOT CONFIGURED: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return False
    successes = 0
    for chat_id in CHAT_IDS:
        try:
            r = SESSION.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)
            payload = r.json()
            if r.ok and payload.get("ok"):
                successes += 1
                log.info("TELEGRAM SENT OK | chat=%s", chat_id)
            else:
                log.error("TELEGRAM ERROR | chat=%s | status=%s | body=%s", chat_id, r.status_code, r.text[:300])
        except Exception:
            log.exception("TELEGRAM EXCEPTION | chat=%s", chat_id)
    return successes == len(CHAT_IDS)


def parse_ts(value):
    ts = int(float(value))
    return ts // 1000 if ts > 10_000_000_000 else ts


def fetch_5m_candles(symbol, limit=300):
    """Fetch native MEXC Futures 5-minute candles (Min5), not aggregated 1m candles."""
    now = int(time.time())
    try:
        response = SESSION.get(API_URL.format(symbol=symbol), params={"interval": "Min5", "limit": limit, "start": now - limit * TIMEFRAME_SECONDS, "end": now}, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        body = getattr(getattr(exc, "response", None), "text", "")[:400]
        raise RuntimeError(f"MEXC Futures 5m request failed for {symbol}: {exc}; body={body}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"MEXC returned non-JSON for {symbol}: {response.text[:250]}") from exc
    if payload.get("success") is False:
        raise RuntimeError(f"MEXC API error for {symbol}: code={payload.get('code')} message={payload.get('message')}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("time"), list):
        raise RuntimeError(f"Unexpected MEXC kline response for {symbol}: {str(payload)[:300]}")
    times, opens, closes = data["time"], data.get("open", []), data.get("close", [])
    if not (len(times) == len(opens) == len(closes)):
        raise RuntimeError(f"MEXC candle arrays have different lengths for {symbol}")
    rows = []
    for ts, op, cl in zip(times, opens, closes):
        rows.append({"ts": parse_ts(ts), "open": float(op), "close": float(cl)})
    by_ts = {c["ts"]: c for c in rows}
    rows = [by_ts[k] for k in sorted(by_ts)]
    closed = [c for c in rows if c["ts"] + TIMEFRAME_SECONDS <= now]
    live = next((c for c in reversed(rows) if c["ts"] <= now < c["ts"] + TIMEFRAME_SECONDS), None)
    return closed[-250:], live


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict): raise ValueError("state must be object")
        return {"pending": state.get("pending", []) if isinstance(state.get("pending", []), list) else []}
    except FileNotFoundError:
        return {"pending": []}
    except Exception as exc:
        log.warning("Cannot read state %s: %s; starting clean", path, exc)
        return {"pending": []}


def save_state(state, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class SignalEngine:
    """Six-candle pattern: first three same color, next three opposite; check #7–#12."""
    def __init__(self, symbol):
        self.symbol = symbol
        self.state_path = os.getenv(f"STATE_FILE_{symbol}", f"state_{symbol}.json")
        self.state = load_state(self.state_path)
        self.pending = [p for p in self.state["pending"] if isinstance(p, dict) and "signal_ts" in p and p.get("direction") in ("LONG", "SHORT")]
        self.candles = OrderedDict()
        self.pre_sent = set()
        self.initialized = False

    @staticmethod
    def pattern(block):
        if len(block) != 6: return None
        colors = [candle_color(c) for c in block]
        if colors[:3] == ["GREEN"] * 3 and colors[3:] == ["RED"] * 3:
            return {"direction": "LONG", "start_color": "GREEN"}
        if colors[:3] == ["RED"] * 3 and colors[3:] == ["GREEN"] * 3:
            return {"direction": "SHORT", "start_color": "RED"}
        return None

    def seed(self, closed):
        self.candles = OrderedDict((c["ts"], c) for c in closed[-250:])
        self.initialized = True
        latest = next(reversed(self.candles.values())) if self.candles else None
        log.info("INITIALIZED | %s | closed_5m=%d | restored_pending=%d%s", self.symbol, len(self.candles), len(self.pending), f" | latest={utc(latest['ts'])} {candle_color(latest)}" if latest else "")

    def pattern_for_live_sixth(self, live):
        first_ts = live["ts"] - 5 * TIMEFRAME_SECONDS
        prior = [self.candles.get(first_ts + i * TIMEFRAME_SECONDS) for i in range(5)]
        if any(c is None for c in prior): return None
        pattern = self.pattern(prior + [live])
        if not pattern: return None
        # The requested rule is based strictly on these six candles.
        # Do not reject the pattern based on candle #0 before this six-candle block.
        return {**pattern, "start_ts": first_ts, "signal_ts": live["ts"]}

    def maybe_pre_alert(self, live):
        if not self.initialized or not live: return
        elapsed = time.time() - live["ts"]
        if not (TIMEFRAME_SECONDS - 60 <= elapsed < TIMEFRAME_SECONDS): return
        ts = live["ts"]
        if ts in self.pre_sent or ts in self.candles: return
        p = self.pattern_for_live_sixth(live)
        if not p: return
        pat = "🔴🔴🔴🟢🟢🟢" if p["direction"] == "SHORT" else "🟢🟢🟢🔴🔴🔴"
        msg = (f"⚠️ PRE-ALERT — {symbol_label(self.symbol)} Futures\nTimeframe: 5m\nPattern: {pat}\n"
               f"Potential direction: {p['direction']}\nCandle #6 closes at {kyiv(ts + TIMEFRAME_SECONDS)} Kyiv time.\nFinal SIGNAL only after close.")
        if send_telegram(msg): self.pre_sent.add(ts)
        log.info("PRE-ALERT | %s | %s", self.symbol, p["direction"])

    def ingest(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        existing = set(self.candles)
        new = [c for c in closed if c["ts"] not in existing]
        for c in sorted(new, key=lambda x: x["ts"]):
            self.candles[c["ts"]] = c
            self.candles = OrderedDict(sorted(self.candles.items()))
            while len(self.candles) > 250: self.candles.popitem(last=False)
            self.evaluate(c["ts"])
        if new:
            self.state["pending"] = self.pending
            save_state(self.state, self.state_path)
        return len(new)

    def evaluate(self, ts):
        keys = list(self.candles)
        idx = keys.index(ts)
        current = self.candles[ts]
        keep = []
        for setup in self.pending:
            rel = (ts - setup["signal_ts"]) // TIMEFRAME_SECONDS
            if rel < 1:
                keep.append(setup); continue
            if rel > 6:
                continue
            number = int(rel) + 6  # rel 1 => candle #7; rel 6 => candle #12
            expected = "RED" if setup["direction"] == "SHORT" else "GREEN"
            if candle_color(current) == expected:
                send_telegram(f"✅ WIN\n{symbol_label(self.symbol)} MEXC Futures | 5m\nDirection: {setup['direction']}\nMatching candle: #{number}/#12 { '🔴' if expected == 'RED' else '🟢' }\nSignal time: {kyiv(setup.get('start_ts', setup['signal_ts']))} Kyiv time.")
                log.info("RESULT WIN | %s | %s | candle #%d", self.symbol, setup["direction"], number)
                continue
            if number == 12:
                send_telegram(f"❌ LOSS\n{symbol_label(self.symbol)} MEXC Futures | 5m\nDirection: {setup['direction']}\nNo matching candle appeared in #7–#12.")
                log.info("RESULT LOSS | %s | %s | after candle #12", self.symbol, setup["direction"])
                continue
            keep.append(setup)
        self.pending = keep
        if idx < 5: return
        block = [self.candles[k] for k in keys[idx-5:idx+1]]
        p = self.pattern(block)
        if not p: return
        start_color = p["start_color"]
        start_ts = block[0]["ts"]
        # Match the exact latest six-candle block; preceding candle color is irrelevant.
        if any(x.get("signal_ts") == ts for x in self.pending): return
        setup = {"start_ts": start_ts, "signal_ts": ts, "direction": p["direction"], "start_color": start_color}
        pat = "🔴🔴🔴🟢🟢🟢" if p["direction"] == "SHORT" else "🟢🟢🟢🔴🔴🔴"
        msg = (f"🔔 SIGNAL {p['direction']}\n\n{symbol_label(self.symbol)} MEXC Futures\nTimeframe: 5m\nPattern: {pat}\n"
               f"Pattern closed: {kyiv(ts + TIMEFRAME_SECONDS)} Kyiv time\nCheck candles #7–#12; first matching candle = WIN, otherwise LOSS after #12.\n\nSignal only — no automatic trading.")
        sent = send_telegram(msg)
        self.pending.append(setup)
        self.state["pending"] = self.pending
        save_state(self.state, self.state_path)
        log.info("SIGNAL %s | %s | sent=%s | result_window=#7-#12", p["direction"], self.symbol, sent)


def run_tests():
    tests = [
        (["GREEN","GREEN","GREEN","RED","RED","RED"], "LONG"),
        (["RED","RED","RED","GREEN","GREEN","GREEN"], "SHORT"),
        (["RED","RED","GREEN","GREEN","GREEN","GREEN"], None),
        (["GREEN","GREEN","GREEN","RED","RED","DOJI"], None),
    ]
    for colors, expected in tests:
        block = [{"open": 1, "close": 0 if c == "RED" else 2 if c == "GREEN" else 1} for c in colors]
        actual = SignalEngine.pattern(block)
        assert (actual["direction"] if actual else None) == expected
        print("PASS pattern", colors, "=>", expected)

    def outcome(direction, colors):
        expected = "RED" if direction == "SHORT" else "GREEN"
        for number, color in enumerate(colors, 7):
            if color == expected: return ("WIN", number)
        return ("LOSS", 12)

    assert outcome("LONG", ["RED"]*5+["GREEN"]) == ("WIN", 12)
    assert outcome("SHORT", ["GREEN"]*6) == ("LOSS", 12)
    assert outcome("LONG", ["GREEN"]+["RED"]*5) == ("WIN", 7)
    assert outcome("SHORT", ["RED"]+["GREEN"]*5) == ("WIN", 7)
    print("PASS result window #7–#12 inclusive")
    print("All offline tests passed")


def main():
    if not SYMBOLS: raise RuntimeError("MEXC_SYMBOLS is empty; set BTCUSDT,ETHUSDT")
    log.info("Started MEXC Futures signal bot for %s on native 5m candles", ", ".join(SYMBOLS))
    log.info("Patterns: 🟢🟢🟢🔴🔴🔴 => LONG; 🔴🔴🔴🟢🟢🟢 => SHORT; result window #7–#12")
    if TOKEN and CHAT_IDS: send_telegram(f"🤖 BOT ONLINE\nMEXC Futures\nSymbols: {', '.join(symbol_label(s) for s in SYMBOLS)}\nTimeframe: 5m\nAlerts only — no automatic orders.")
    engines = {s: SignalEngine(s) for s in SYMBOLS}
    heartbeat = {}
    while True:
        for symbol, engine in engines.items():
            try:
                closed, live = fetch_5m_candles(symbol)
                if not closed:
                    log.warning("%s | MEXC returned no complete closed 5m candles", symbol); continue
                if not engine.initialized:
                    engine.seed(closed)
                else:
                    added = engine.ingest(closed)
                    if added: log.info("NEW DATA | %s | closed_5m=%d | added=%d | latest=%s %s | pending=%d", symbol, len(closed), added, utc(closed[-1]["ts"]), candle_color(closed[-1]), len(engine.pending))
                if live: engine.maybe_pre_alert(live)
                if time.time() - heartbeat.get(symbol, 0) >= 60:
                    log.info("HEARTBEAT OK | %s | latest_closed=%s | pending=%d", symbol, utc(closed[-1]["ts"]), len(engine.pending))
                    heartbeat[symbol] = time.time()
            except Exception:
                log.exception("LOOP ERROR | %s", symbol)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    if TEST_MODE:
        run_tests()
        print("Timeframe: 5m | MEXC native Min5 | PRE final minute of #6 | SIGNAL after #6 close | WIN/LOSS #7–#12")
    else:
        main()
