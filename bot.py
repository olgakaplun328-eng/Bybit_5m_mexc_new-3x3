import json
import logging
import os
import time
import tempfile
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

API_URL = os.getenv(
    "MEXC_FUTURES_KLINE_URL",
    "https://api.mexc.com/api/v1/contract/kline/{symbol}",
).strip()
# Normalize a legacy Railway variable so it cannot silently keep the retired host.
API_URL = API_URL.replace("https://contract.mexc.com", "https://api.mexc.com")

SYMBOLS = list(dict.fromkeys(
    f"{s.strip().upper().replace('_', '')[:-4]}_USDT"
    if s.strip().upper().replace("_", "").endswith("USDT")
    else f"{s.strip().upper().replace('_', '')}_USDT"
    for s in os.getenv("MEXC_SYMBOLS", DEFAULT_SYMBOLS).split(",")
    if s.strip()
))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("mexc-futures-signal-bot-5m")
SESSION = requests.Session()
SESSION.headers.update({
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": "MEXC-Futures-5m-Signal-Bot/7.0",
})


def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M")


def symbol_label(symbol):
    return symbol.replace("_", "")


def candle_color(candle):
    if candle["close"] > candle["open"]:
        return "GREEN"
    if candle["close"] < candle["open"]:
        return "RED"
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
            response = SESSION.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            payload = response.json()
            if response.ok and payload.get("ok"):
                successes += 1
                log.info("TELEGRAM SENT OK | chat=%s", chat_id)
            else:
                log.error(
                    "TELEGRAM ERROR | chat=%s | status=%s | body=%s",
                    chat_id, response.status_code, response.text[:300],
                )
        except Exception:
            log.exception("TELEGRAM EXCEPTION | chat=%s", chat_id)
    return successes == len(CHAT_IDS)


def parse_ts(value):
    ts = int(float(value))
    return ts // 1000 if ts > 10_000_000_000 else ts


def fetch_5m_candles(symbol, limit=300):
    """Fetch native MEXC Futures 5-minute candles (Min5)."""
    now = int(time.time())
    try:
        response = SESSION.get(
            API_URL.format(symbol=symbol),
            params={
                "interval": "Min5",
                "limit": limit,
                "start": now - limit * TIMEFRAME_SECONDS,
                "end": now,
            },
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        body = getattr(getattr(exc, "response", None), "text", "")[:400]
        raise RuntimeError(
            f"MEXC Futures 5m request failed for {symbol}: {exc}; body={body}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"MEXC returned non-JSON for {symbol}: {response.text[:250]}"
        ) from exc

    if payload.get("success") is False:
        raise RuntimeError(
            f"MEXC API error for {symbol}: code={payload.get('code')} "
            f"message={payload.get('message')}"
        )

    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("time"), list):
        raise RuntimeError(
            f"Unexpected MEXC kline response for {symbol}: {str(payload)[:300]}"
        )

    times, opens, closes = data["time"], data.get("open", []), data.get("close", [])
    if not (len(times) == len(opens) == len(closes)):
        raise RuntimeError(f"MEXC candle arrays have different lengths for {symbol}")

    rows = [
        {"ts": parse_ts(ts), "open": float(op), "close": float(cl)}
        for ts, op, cl in zip(times, opens, closes)
    ]
    by_ts = {candle["ts"]: candle for candle in rows}
    rows = [by_ts[key] for key in sorted(by_ts)]
    closed = [c for c in rows if c["ts"] + TIMEFRAME_SECONDS <= now]
    live = next(
        (c for c in reversed(rows) if c["ts"] <= now < c["ts"] + TIMEFRAME_SECONDS),
        None,
    )
    return closed[-250:], live


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            state = json.load(file)
        if not isinstance(state, dict):
            raise ValueError("state must be object")
        pending = state.get("pending", [])
        return {"pending": pending if isinstance(pending, list) else []}
    except FileNotFoundError:
        return {"pending": []}
    except Exception as exc:
        log.warning("Cannot read state %s: %s; starting clean", path, exc)
        return {"pending": []}


def save_state(state, path):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


class SignalEngine:
    """Six-candle pattern: three candles of one color, then three of the opposite color."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.state_path = os.getenv(f"STATE_FILE_{symbol}", f"state_{symbol}.json")
        self.state = load_state(self.state_path)
        self.pending = [
            p for p in self.state["pending"]
            if isinstance(p, dict) and "signal_ts" in p
            and p.get("direction") in ("LONG", "SHORT")
        ]
        self.candles = OrderedDict()
        self.pre_sent = set()
        self.initialized = False

    @staticmethod
    def pattern(block):
        if len(block) != 6:
            return None
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
        suffix = f" | latest={utc(latest['ts'])} {candle_color(latest)}" if latest else ""
        log.info(
            "INITIALIZED | %s | closed_5m=%d | restored_pending=%d%s",
            self.symbol, len(self.candles), len(self.pending), suffix,
        )

    def pattern_for_live_sixth(self, live):
        first_ts = live["ts"] - 5 * TIMEFRAME_SECONDS
        prior = [
            self.candles.get(first_ts + i * TIMEFRAME_SECONDS)
            for i in range(5)
        ]
        if any(c is None for c in prior):
            return None
        pattern = self.pattern(prior + [live])
        if not pattern:
            return None
        # Only the six candles in this block define the pattern.
        return {**pattern, "start_ts": first_ts, "signal_ts": live["ts"]}

    def maybe_pre_alert(self, live):
        if not self.initialized or not live:
            return
        elapsed = time.time() - live["ts"]
        if not (TIMEFRAME_SECONDS - 60 <= elapsed < TIMEFRAME_SECONDS):
            return
        ts = live["ts"]
        if ts in self.pre_sent or ts in self.candles:
            return
        pattern = self.pattern_for_live_sixth(live)
        if not pattern:
            return

        pattern_text = (
            "ð´ð´ð´ð¢ð¢ð¢"
            if pattern["direction"] == "SHORT"
            else "ð¢ð¢ð¢ð´ð´ð´"
        )
        message = (
            f"â ï¸ PRE-ALERT â {symbol_label(self.symbol)} Futures\n"
            f"Timeframe: 5m\nPattern: {pattern_text}\n"
            f"Potential direction: {pattern['direction']}\n"
            f"Candle #6 closes at {kyiv(ts + TIMEFRAME_SECONDS)} Kyiv time.\n"
            "Final SIGNAL only after close."
        )
        if send_telegram(message):
            self.pre_sent.add(ts)
        log.info("PRE-ALERT | %s | %s", self.symbol, pattern["direction"])

    def ingest(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        existing = set(self.candles)
        new_candles = [c for c in closed if c["ts"] not in existing]
        for candle in sorted(new_candles, key=lambda c: c["ts"]):
            self.candles[candle["ts"]] = candle
            self.candles = OrderedDict(sorted(self.candles.items()))
            while len(self.candles) > 250:
                self.candles.popitem(last=False)
            self.evaluate(candle["ts"])
        if new_candles:
            self.state["pending"] = self.pending
            save_state(self.state, self.state_path)
        return len(new_candles)

    def evaluate(self, ts):
        keys = list(self.candles)
        index = keys.index(ts)
        current = self.candles[ts]
        keep = []

        for setup in self.pending:
            rel = (ts - setup["signal_ts"]) // TIMEFRAME_SECONDS
            if rel < 1:
                keep.append(setup)
                continue
            if rel > 7:
                continue

            number = int(rel) + 6  # rel 1 => candle #7; rel 7 => candle #13
            expected = "RED" if setup["direction"] == "SHORT" else "GREEN"
            if candle_color(current) == expected:
                emoji = "ð´" if expected == "RED" else "ð¢"
                send_telegram(
                    f"â WIN\n{symbol_label(self.symbol)} MEXC Futures | 5m\n"
                    f"Direction: {setup['direction']}\n"
                    f"Matching candle: #{number}/#13 {emoji}\n"
                    f"Signal time: {kyiv(setup.get('start_ts', setup['signal_ts']))} Kyiv time."
                )
                log.info(
                    "RESULT WIN | %s | %s | candle #%d",
                    self.symbol, setup["direction"], number,
                )
                continue

            if number == 13:
                send_telegram(
                    f"â LOSS\n{symbol_label(self.symbol)} MEXC Futures | 5m\n"
                    f"Direction: {setup['direction']}\n"
                    "No matching candle appeared in #7â#13."
                )
                log.info(
                    "RESULT LOSS | %s | %s | after candle #13",
                    self.symbol, setup["direction"],
                )
                continue
            keep.append(setup)

        self.pending = keep
        if index < 5:
            return

        block = [self.candles[k] for k in keys[index - 5:index + 1]]
        pattern = self.pattern(block)
        if not pattern:
            return
        if any(item.get("signal_ts") == ts for item in self.pending):
            return

        setup = {
            "start_ts": block[0]["ts"],
            "signal_ts": ts,
            "direction": pattern["direction"],
            "start_color": pattern["start_color"],
        }
        pattern_text = (
            "ð´ð´ð´ð¢ð¢ð¢"
            if pattern["direction"] == "SHORT"
            else "ð¢ð¢ð¢ð´ð´ð´"
        )
        message = (
            f"ð SIGNAL {pattern['direction']}\n\n"
            f"{symbol_label(self.symbol)} MEXC Futures\n"
            "Timeframe: 5m\n"
            f"Pattern: {pattern_text}\n"
            f"Pattern closed: {kyiv(ts + TIMEFRAME_SECONDS)} Kyiv time\n"
            "Check candles #7â#13; first matching candle = WIN, otherwise LOSS after #13.\n\n"
            "Signal only â no automatic trading."
        )
        sent = send_telegram(message)
        self.pending.append(setup)
        self.state["pending"] = self.pending
        save_state(self.state, self.state_path)
        log.info(
            "SIGNAL %s | %s | sent=%s | result_window=#7-#13",
            pattern["direction"], self.symbol, sent,
        )


def run_tests():
    tests = [
        (["GREEN", "GREEN", "GREEN", "RED", "RED", "RED"], "LONG"),
        (["RED", "RED", "RED", "GREEN", "GREEN", "GREEN"], "SHORT"),
        (["RED", "RED", "GREEN", "GREEN", "GREEN", "GREEN"], None),
        (["GREEN", "GREEN", "GREEN", "RED", "RED", "DOJI"], None),
    ]
    for colors, expected in tests:
        block = [
            {
                "ts": i * TIMEFRAME_SECONDS,
                "open": 1,
                "close": 0 if color == "RED" else 2 if color == "GREEN" else 1,
            }
            for i, color in enumerate(colors)
        ]
        actual = SignalEngine.pattern(block)
        assert (actual["direction"] if actual else None) == expected
        emoji = {"GREEN": "ð¢", "RED": "ð´", "DOJI": "â"}
        print("PASS pattern", "".join(emoji[c] for c in colors), "=>", expected)

    def outcome(direction, colors):
        expected = "RED" if direction == "SHORT" else "GREEN"
        for number, color in enumerate(colors, 7):
            if color == expected:
                return ("WIN", number)
        return ("LOSS", 13)

    assert outcome("LONG", ["RED"] * 6 + ["GREEN"]) == ("WIN", 13)
    assert outcome("SHORT", ["GREEN"] * 7) == ("LOSS", 13)
    assert outcome("LONG", ["GREEN"] + ["RED"] * 6) == ("WIN", 7)
    assert outcome("SHORT", ["RED"] + ["GREEN"] * 6) == ("WIN", 7)
    print("PASS result window #7â#13 inclusive")

    # Offline integration check: simulate PRE and SIGNAL without network calls.
    captured = []
    original_send = send_telegram
    original_time = time.time
    try:
        globals()["send_telegram"] = lambda message: captured.append(message) or True
        base = 1_800_000_000 - (1_800_000_000 % TIMEFRAME_SECONDS)
        colors = ["GREEN", "GREEN", "GREEN", "RED", "RED", "RED"]
        candles = [
            {
                "ts": base + i * TIMEFRAME_SECONDS,
                "open": 100.0,
                "close": 101.0 if color == "GREEN" else 99.0,
            }
            for i, color in enumerate(colors)
        ]

        pre_engine = SignalEngine.__new__(SignalEngine)
        pre_engine.symbol = "BTC_USDT"
        pre_engine.candles = OrderedDict((c["ts"], c) for c in candles[:5])
        pre_engine.pre_sent = set()
        pre_engine.initialized = True
        globals()["time"].time = lambda: candles[5]["ts"] + 270
        pre_engine.maybe_pre_alert(candles[5])
        assert any(
            "PRE-ALERT" in m and "LONG" in m and "ð¢ð¢ð¢ð´ð´ð´" in m
            for m in captured
        ), captured
        print("PASS PRE simulation: LONG PRE-ALERT printed during final minute of candle #6")

        globals()["time"].time = original_time
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = SignalEngine.__new__(SignalEngine)
            engine.symbol = "BTC_USDT"
            engine.state_path = os.path.join(temp_dir, "state.json")
            engine.state = {"pending": []}
            engine.pending = []
            engine.candles = OrderedDict((c["ts"], c) for c in candles[:5])
            engine.pre_sent = set()
            engine.initialized = True
            engine.ingest(candles)
            assert any(
                "SIGNAL LONG" in m and "ð¢ð¢ð¢ð´ð´ð´" in m and "#7â#13" in m
                for m in captured
            ), captured
            print("PASS SIGNAL simulation: LONG SIGNAL printed after candle #6 closes")
    finally:
        globals()["send_telegram"] = original_send
        globals()["time"].time = original_time

    print("All offline tests passed. No live MEXC request or Telegram message was sent.")


def main():
    if not SYMBOLS:
        raise RuntimeError("MEXC_SYMBOLS is empty; set BTCUSDT,ETHUSDT")
    log.info(
        "Started MEXC Futures signal bot for %s on native 5m candles",
        ", ".join(SYMBOLS),
    )
    log.info(
        "Patterns: ð¢ð¢ð¢ð´ð´ð´ => LONG; ð´ð´ð´ð¢ð¢ð¢ => SHORT; "
        "result window #7â#13"
    )
    if TOKEN and CHAT_IDS:
        send_telegram(
            f"ð¤ BOT ONLINE\nMEXC Futures\n"
            f"Symbols: {', '.join(symbol_label(s) for s in SYMBOLS)}\n"
            "Timeframe: 5m\nAlerts only â no automatic orders."
        )

    engines = {symbol: SignalEngine(symbol) for symbol in SYMBOLS}
    heartbeat = {}
    while True:
        for symbol, engine in engines.items():
            try:
                closed, live = fetch_5m_candles(symbol)
                if not closed:
                    log.warning("%s | MEXC returned no complete closed 5m candles", symbol)
                    continue
                if not engine.initialized:
                    engine.seed(closed)
                else:
                    added = engine.ingest(closed)
                    if added:
                        log.info(
                            "NEW DATA | %s | closed_5m=%d | added=%d | latest=%s %s | pending=%d",
                            symbol, len(closed), added, utc(closed[-1]["ts"]),
                            candle_color(closed[-1]), len(engine.pending),
                        )
                if live:
                    engine.maybe_pre_alert(live)
                if time.time() - heartbeat.get(symbol, 0) >= 60:
                    log.info(
                        "HEARTBEAT OK | %s | latest_closed=%s | pending=%d",
                        symbol, utc(closed[-1]["ts"]), len(engine.pending),
                    )
                    heartbeat[symbol] = time.time()
            except Exception:
                log.exception("LOOP ERROR | %s", symbol)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if TEST_MODE:
        run_tests()
        print(
            "Timeframe: 5m | MEXC native Min5 | PRE final minute of #6 | "
            "SIGNAL after #6 close | WIN/LOSS #7â#13"
        )
    else:
        main()
