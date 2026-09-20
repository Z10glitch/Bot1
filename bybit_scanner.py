#!/usr/bin/env python3
"""
Bybit perp alert bot — A+ setups only, via Telegram.

Replaces Bot1's DEXScreener low-cap discovery with the market Zach actually
trades. Same rails: GitHub Actions cron, Telegram delivery, state committed
back to the repo.

Design rules, learned the hard way:

  * TWO TIMEFRAMES. A 24h snapshot cannot see that something just double-topped
    and rolled over. Every candidate is confirmed on 15m (entry timing) AND
    4h (trend) before it can fire. This is the single most important gate.

  * SILENCE IS A VALID OUTPUT. A+ means A+. Most runs send nothing. A scanner
    that alerts every cycle is a scanner you stop reading.

  * NEVER FAIL QUIETLY. Bot1's cluster signal never fired for months because a
    parse bug returned [] with no error. Every failure path here logs loudly
    and the run summary prints what was evaluated and why it was rejected.

  * FIRE ON TRANSITIONS, NOT STATES. Regime alerts fire when the market CHANGES
    posture, not every 15 minutes while it stays there.

  * COOLDOWNS. One alert per symbol per COOLDOWN_HOURS, so a coin that sits in
    a valid setup doesn't spam you every cycle.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (both required)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------- config ----
# Everything tunable lives in config.json so you never have to edit this file.
# If config.json is missing or malformed, these defaults are used and the run
# says so loudly rather than silently behaving differently than you expect.

BYBIT = "https://api.bybit.com"

CONFIG_FILE = "config.json"
STATE_FILE = "bot_state.json"

DEFAULTS = {
    "min_turnover_usd": 5_000_000,
    "min_risk_reward": 1.8,
    "max_alerts_per_run": 3,
    "cooldown_hours": 4,
    "max_24h_move_pct": 40.0,
    "thin_book_warn_usd": 20_000_000,
    "short_min_off_low_pct": 1.5,
    "short_max_funding_cost_pct_day": 0.20,
    "enable_longs": True,
    "enable_shorts": True,
    "enable_regime_alerts": True,
    "quiet_hours": {"enabled": False, "from": 23, "to": 7},
    "timezone": "UTC",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        print(f"!! {CONFIG_FILE} not found — using built-in defaults.", flush=True)
        return cfg
    except json.JSONDecodeError as e:
        print(f"!! {CONFIG_FILE} is not valid JSON ({e}). Using built-in defaults. "
              f"Check for a missing comma or a trailing comma.", flush=True)
        return cfg

    unknown = [k for k in raw if k != "_help" and k not in DEFAULTS]
    if unknown:
        print(f"!! ignoring unknown config keys: {unknown}", flush=True)
    for k in DEFAULTS:
        if k in raw:
            cfg[k] = raw[k]
    return cfg


CFG = load_config()

MIN_TURNOVER = CFG["min_turnover_usd"]
THIN_BOOK_WARN = CFG["thin_book_warn_usd"]
MAX_24H_MOVE = CFG["max_24h_move_pct"]
MIN_RR = CFG["min_risk_reward"]
COOLDOWN_HOURS = CFG["cooldown_hours"]
MAX_ALERTS_PER_RUN = CFG["max_alerts_per_run"]
SHORT_MIN_OFF_LOW = CFG["short_min_off_low_pct"]
SHORT_MAX_FUNDING_COST = CFG["short_max_funding_cost_pct_day"]


def in_quiet_hours():
    """True if setup alerts should be held back right now. FLUSH regime alerts
    ignore this — if the market is falling apart you want to know."""
    q = CFG.get("quiet_hours") or {}
    if not q.get("enabled"):
        return False
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(CFG.get("timezone", "UTC")))
    except Exception as e:
        print(f"!! timezone '{CFG.get('timezone')}' unusable ({e}) — quiet hours off.", flush=True)
        return False
    h, start, end = now.hour, int(q.get("from", 23)), int(q.get("to", 7))
    return (start <= h or h < end) if start > end else (start <= h < end)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# ------------------------------------------------------------- utilities ----

def log(msg):
    print(msg, flush=True)


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("!! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — cannot send. "
            "Alert was generated but went nowhere.")
        log(text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code != 200:
            log(f"!! Telegram send failed {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"!! Telegram send raised: {e}")
        return False


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {"last_alert": {}, "regime": None}


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ------------------------------------------------------------ market data ---

def get_tickers():
    r = requests.get(f"{BYBIT}/v5/market/tickers", params={"category": "linear"}, timeout=25)
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"tickers error: {d.get('retMsg')}")
    return d["result"]["list"]


def get_klines(symbol, interval, limit=100):
    try:
        r = requests.get(f"{BYBIT}/v5/market/kline",
                         params={"category": "linear", "symbol": symbol,
                                 "interval": interval, "limit": limit},
                         timeout=20)
        r.raise_for_status()
        d = r.json()
        if d.get("retCode") != 0:
            log(f"!! kline {symbol} {interval}m: {d.get('retMsg')}")
            return []
        rows = list(reversed(d["result"]["list"]))
        return [{"o": float(x[1]), "h": float(x[2]),
                 "l": float(x[3]), "c": float(x[4])} for x in rows]
    except Exception as e:
        log(f"!! kline fetch {symbol} {interval}m failed: {e}")
        return []


# ------------------------------------------------------------ indicators ----

def sma(vals, p):
    return sum(vals[-p:]) / p if len(vals) >= p else None


def ema_series(vals, p):
    if len(vals) < p:
        return []
    k = 2 / (p + 1)
    out = [sum(vals[:p]) / p]
    for v in vals[p:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes):
    if len(closes) < 35:
        return None
    ef, es = ema_series(closes, 12), ema_series(closes, 26)
    ef = ef[26 - 12:]
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, 9)
    if len(dea) < 2:
        return None
    dt = dif[-len(dea):]
    hist = [d - s for d, s in zip(dt, dea)]
    return {"bull": dt[-1] > dea[-1], "rising": hist[-1] > hist[-2]}


def atr(k, p=14):
    if len(k) < p + 1:
        return None
    tr = []
    for i in range(1, len(k)):
        pc = k[i - 1]["c"]
        tr.append(max(k[i]["h"] - k[i]["l"], abs(k[i]["h"] - pc), abs(k[i]["l"] - pc)))
    return sum(tr[-p:]) / p


def frame(k):
    """Everything we need from one timeframe's candles."""
    if len(k) < 40:
        return None
    c = [x["c"] for x in k]
    last = c[-1]
    m7, m14, m28 = sma(c, 7), sma(c, 14), sma(c, 28)
    m = macd(c)
    if not (m7 and m14 and m28 and m):
        return None
    recent = k[-24:]
    sw_hi = max(x["h"] for x in recent)
    sw_lo = min(x["l"] for x in recent)

    rejection = False
    for x in k[-4:]:
        rng = x["h"] - x["l"]
        if rng <= 0:
            continue
        upper = x["h"] - max(x["o"], x["c"])
        if x["h"] >= sw_hi * 0.998 and upper / rng > 0.45:
            rejection = True

    return {
        "last": last, "ma7": m7, "ma14": m14, "ma28": m28,
        "above": sum([last > m7, last > m14, last > m28]),
        "below": sum([last < m7, last < m14, last < m28]),
        "stacked_up": m7 > m14 > m28,
        "stacked_dn": m7 < m14 < m28,
        "bull": m["bull"], "rising": m["rising"],
        "sw_hi": sw_hi, "sw_lo": sw_lo,
        "fade_from_high": (sw_hi - last) / sw_hi * 100,
        "off_low": (last - sw_lo) / sw_lo * 100 if sw_lo else 0,
        "rejection": rejection,
        "atr": atr(k),
    }


# ------------------------------------------------------------- A+ grading ---

def grade_long(sym, t, f15, f4h):
    """Returns an alert dict, or None with the rejection reason logged."""
    why = []

    if f15["above"] < 3:
        return None, f"15m only {f15['above']}/3 MAs"
    if not (f15["bull"] and f15["rising"]):
        return None, "15m momentum not expanding"
    if f15["rejection"]:
        return None, "15m rejection wick off the high"
    if f15["fade_from_high"] > 2.0:
        return None, f"already faded {f15['fade_from_high']:.1f}% off 15m high"

    if f4h["above"] < 3:
        return None, f"4h only {f4h['above']}/3 MAs"
    if not f4h["stacked_up"]:
        return None, "4h MAs not stacked (no established uptrend)"
    if not f4h["bull"]:
        return None, "4h MACD bearish"

    if abs(t["pct24h"]) > MAX_24H_MOVE:
        return None, f"parabolic ({t['pct24h']:+.0f}% 24h)"

    entry = f15["last"]
    stop = f15["sw_lo"] * 0.998
    risk = entry - stop
    if risk <= 0:
        return None, "invalid stop"
    # The 4h swing high is the natural target — but a coin breaking to new highs
    # has its swing high sitting right on top of price, which would score a
    # near-zero R:R on the best setups. When there's no meaningful overhead
    # reference, fall back to a measured move off the risk.
    target = f4h["sw_hi"]
    if target < entry + risk * MIN_RR:
        target = entry + risk * 2.5
    rr = (target - entry) / risk
    if rr < MIN_RR:
        return None, f"R:R only {rr:.1f} (need {MIN_RR})"

    why.append("15m: above all MAs, momentum expanding")
    why.append("4h: uptrend intact, MAs stacked")
    if f4h["rising"]:
        why.append("4h momentum still building")

    return {
        "sym": sym, "side": "LONG", "entry": entry, "stop": stop,
        "target": target, "rr": rr, "risk_pct": risk / entry * 100,
        "why": why, "turnover": t["turnover"], "pct24h": t["pct24h"],
        "funding_day": t["funding_day"], "atr4h_pct": (f4h["atr"] / entry * 100) if f4h["atr"] else None,
    }, None


def grade_short(sym, t, f15, f4h):
    # Note the interaction here: requiring "below ALL 3 MAs" and "bounced off
    # the low" is nearly self-contradictory — a bounce big enough to be worth
    # shorting usually lifts price back through MA7. So the gate is below the
    # two slower MAs, which is the failed-retest shape: trend broken, price
    # rallied back into the MA zone, momentum still down.
    if f15["below"] < 2:
        return None, f"15m only {f15['below']}/3 MAs below"
    # Only require that momentum is no longer building. Demanding a completed
    # bearish MACD cross (dif < dea) is lagging: after a bounce, dif sits above
    # dea for a while even as the histogram rolls over, so that condition
    # directly contradicted the "must be off the low" rule and the short could
    # never fire. Mirror of the long gate, which requires bull AND rising.
    if f15["rising"]:
        return None, "15m momentum still building"
    if f15["off_low"] < SHORT_MIN_OFF_LOW:
        return None, f"only {f15['off_low']:.1f}% off swing low — shorting into support"

    if f4h["above"] >= 2:
        return None, "4h trend still up — don't fight it"
    if f4h["stacked_up"]:
        return None, "4h MAs still stacked bullish"
    if f4h["bull"] and f4h["rising"]:
        return None, "4h momentum building against the short"

    if t["funding_day"] < -SHORT_MAX_FUNDING_COST:
        return None, f"short bleeds {abs(t['funding_day']):.2f}%/day in funding"
    if abs(t["pct24h"]) > MAX_24H_MOVE:
        return None, f"parabolic move ({t['pct24h']:+.0f}% 24h)"

    entry = f15["last"]
    stop = f15["sw_hi"] * 1.002
    risk = stop - entry
    if risk <= 0:
        return None, "invalid stop"
    target = f4h["sw_lo"]
    if target > entry - risk * MIN_RR:
        target = entry - risk * 2.5
    rr = (entry - target) / risk
    if rr < MIN_RR:
        return None, f"R:R only {rr:.1f} (need {MIN_RR})"

    why = [f"15m: below {f15['below']}/3 MAs, momentum rolling over",
           f"4h: trend broken ({f4h['above']}/3 MAs), bounced {f15['off_low']:.1f}% off the low"]
    if t["funding_day"] > 0:
        why.append(f"shorts collect {t['funding_day']:.3f}%/day")

    return {
        "sym": sym, "side": "SHORT", "entry": entry, "stop": stop,
        "target": target, "rr": rr, "risk_pct": risk / entry * 100,
        "why": why, "turnover": t["turnover"], "pct24h": t["pct24h"],
        "funding_day": t["funding_day"], "atr4h_pct": (f4h["atr"] / entry * 100) if f4h["atr"] else None,
    }, None


# ------------------------------------------------------------- formatting ---

def money(x):
    if x >= 1e9:
        return f"${x/1e9:.1f}B"
    if x >= 1e6:
        return f"${x/1e6:.0f}M"
    return f"${x/1e3:.0f}K"


def px(v):
    return f"{v:.8g}"


def format_alert(a):
    arrow = "🟢 LONG" if a["side"] == "LONG" else "🔴 SHORT"
    lines = [
        f"{arrow}  <b>{a['sym']}</b>   ({a['pct24h']:+.1f}% 24h)",
        "",
        f"Entry   {px(a['entry'])}",
        f"Stop    {px(a['stop'])}   ({a['risk_pct']:.1f}%)",
        f"Target  {px(a['target'])}   ({a['rr']:.1f}R)",
        "",
    ]
    lines += [f"• {w}" for w in a["why"]]
    lines.append("")
    lines.append(f"Liquidity {money(a['turnover'])} · funding {a['funding_day']:+.3f}%/day")

    # leverage guidance — a wide stop and high leverage means liquidation
    # lands before the stop ever fires
    if a["risk_pct"] > 0:
        max_lev = int(max(1, min(20, 70 / a["risk_pct"])))
        lines.append(f"Max sensible leverage ≈ {max_lev}x (stop is {a['risk_pct']:.1f}% away)")

    # A very high R:R means the target is a structural level a long way off,
    # not somewhere price gets to on a scalp. Say so, rather than implying the
    # whole move is the expectation.
    if a["rr"] >= 4:
        if a["side"] == "LONG":
            partial = a["entry"] + (a["entry"] - a["stop"]) * 2
        else:
            partial = a["entry"] - (a["stop"] - a["entry"]) * 2
        lines.append(f"Target is a structural level — consider partials at "
                     f"{px(partial)} (2R) and trailing the rest.")

    if a["turnover"] < THIN_BOOK_WARN:
        lines.append("")
        lines.append("⚠️ Thin book — expect slippage and stop-hunt wicks. Size down.")

    return "\n".join(lines)


def format_regime(new, old, detail):
    icons = {"RISK_ON": "📈", "RISK_OFF": "📉", "NEUTRAL": "➡️", "FLUSH": "🚨"}
    head = {
        "RISK_ON": "Market turning risk-on",
        "RISK_OFF": "Market turning risk-off",
        "NEUTRAL": "Market going neutral",
        "FLUSH": "Market dropping hard",
    }[new]
    return (f"{icons[new]} <b>{head}</b>\n"
            f"<i>was {old or 'unknown'}</i>\n\n{detail}")


# ------------------------------------------------------------------- main ---

def main():
    started = datetime.now(timezone.utc)
    log(f"=== Bybit A+ scan @ {started:%Y-%m-%d %H:%M UTC} ===")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("!! Telegram secrets missing — alerts will print to log only.")

    state = load_state()
    now_ts = time.time()

    raw = get_tickers()
    universe = []
    for x in raw:
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        turnover = fnum(x.get("turnover24h"))
        if turnover < MIN_TURNOVER:
            continue
        last = fnum(x.get("lastPrice"))
        hi, lo = fnum(x.get("highPrice24h")), fnum(x.get("lowPrice24h"))
        span = hi - lo
        fr = fnum(x.get("fundingRate"))
        fih = fnum(x.get("fundingIntervalHour"), 8) or 8
        universe.append({
            "sym": s, "last": last, "pct24h": fnum(x.get("price24hPcnt")) * 100,
            "turnover": turnover, "oi": fnum(x.get("openInterestValue")),
            "range_pos": (last - lo) / span if span > 0 else 0.5,
            "funding_day": fr * (24 / fih) * 100,
        })

    log(f"Universe: {len(universe)} symbols above {money(MIN_TURNOVER)}")
    if not universe:
        log("!! Empty universe — check MIN_TURNOVER or the tickers response.")
        return

    # ---------------- regime ----------------
    up = sum(1 for u in universe if u["pct24h"] > 0)
    breadth = up / len(universe) * 100

    major_state, major_bits = [], []
    for m in MAJORS:
        k = get_klines(m, "240", 120)
        f = frame(k)
        if f:
            major_state.append(f["above"])
            major_bits.append(f"{m.replace('USDT','')} {f['above']}/3 MAs")
        time.sleep(0.1)
    majors_strong = sum(1 for a in major_state if a == 3)
    majors_broken = sum(1 for a in major_state if a <= 1)

    # fast flush detector: majors' last hour on 15m candles
    flush = False
    for m in MAJORS[:2]:
        k = get_klines(m, "15", 8)
        if len(k) >= 5:
            hr = (k[-1]["c"] - k[-5]["c"]) / k[-5]["c"] * 100
            log(f"  {m} 1h change: {hr:+.2f}%")
            if hr <= -2.0:
                flush = True
        time.sleep(0.1)

    if flush:
        regime = "FLUSH"
    elif breadth >= 62 and majors_strong >= 2:
        regime = "RISK_ON"
    elif breadth <= 35 or majors_broken >= 2:
        regime = "RISK_OFF"
    else:
        regime = "NEUTRAL"

    log(f"Breadth {breadth:.0f}% up | majors: {', '.join(major_bits)} | regime={regime} "
        f"(was {state.get('regime')})")

    if regime != state.get("regime") and CFG["enable_regime_alerts"]:
        detail = (f"Breadth: {up}/{len(universe)} up ({breadth:.0f}%)\n"
                  f"Majors: {', '.join(major_bits) if major_bits else 'n/a'}")
        if regime == "FLUSH":
            detail += "\n\nMajors down >2% in the last hour. Longs are getting cut — " \
                      "don't catch this, wait for it to stop bleeding."
        elif regime == "RISK_OFF":
            detail += "\n\nBreadth negative and majors losing their MAs. Longs get " \
                      "harder from here; shorts start working."
        elif regime == "RISK_ON":
            detail += "\n\nBroad bid with majors leading. Long setups have tailwind."
        # A FLUSH always goes through — if the market is falling apart at 3am
        # and you're holding something, you want that one. Other regime changes
        # respect quiet hours.
        if regime == "FLUSH" or not in_quiet_hours():
            send_telegram(format_regime(regime, state.get("regime"), detail))
        else:
            log(f"Quiet hours — regime change to {regime} logged, not sent.")
        state["regime"] = regime

    # ---------------- candidate shortlist ----------------
    longs = sorted([u for u in universe
                    if u["pct24h"] > 2 and 0.35 < u["range_pos"] < 0.95],
                   key=lambda u: u["turnover"], reverse=True)[:18]
    shorts = sorted([u for u in universe
                     if u["pct24h"] < 1 and u["range_pos"] < 0.5],
                    key=lambda u: u["turnover"], reverse=True)[:14]

    seen, shortlist = set(), []
    for u in longs + shorts:
        if u["sym"] not in seen:
            seen.add(u["sym"])
            shortlist.append(u)
    log(f"Shortlist: {len(shortlist)} candidates to confirm on 15m + 4h")

    # ---------------- confirm ----------------
    alerts, rejected = [], []
    for u in shortlist:
        sym = u["sym"]
        last_alert = state["last_alert"].get(sym, 0)
        if now_ts - last_alert < COOLDOWN_HOURS * 3600:
            rejected.append((sym, "cooldown"))
            continue

        k15 = get_klines(sym, "15", 80)
        time.sleep(0.08)
        f15 = frame(k15)
        if not f15:
            rejected.append((sym, "no 15m data"))
            continue

        k4 = get_klines(sym, "240", 120)
        time.sleep(0.08)
        f4h = frame(k4)
        if not f4h:
            rejected.append((sym, "no 4h data"))
            continue

        long_reason = short_reason = "disabled in config"
        if CFG["enable_longs"]:
            a, long_reason = grade_long(sym, u, f15, f4h)
            if a:
                alerts.append(a)
                continue
        if CFG["enable_shorts"]:
            a, short_reason = grade_short(sym, u, f15, f4h)
            if a:
                alerts.append(a)
                continue

        rejected.append((sym, f"L:{long_reason} / S:{short_reason}"))

    # best first: highest R:R, liquidity as tiebreak
    alerts.sort(key=lambda a: (a["rr"], a["turnover"]), reverse=True)

    log(f"\n--- {len(alerts)} A+ setup(s), {len(rejected)} rejected ---")
    for sym, why in rejected[:25]:
        log(f"  reject {sym}: {why}")

    quiet = in_quiet_hours()
    if quiet and alerts:
        log(f"Quiet hours ({CFG['quiet_hours']['from']}:00-{CFG['quiet_hours']['to']}:00 "
            f"{CFG['timezone']}) — holding {len(alerts)} setup alert(s).")

    for a in alerts[:MAX_ALERTS_PER_RUN]:
        log(f"  ALERT {a['side']} {a['sym']} rr={a['rr']:.1f}")
        if quiet:
            continue
        if send_telegram(format_alert(a)):
            state["last_alert"][a["sym"]] = now_ts

    if not alerts:
        log("No A+ setups this run. Sending nothing — that's working as intended.")

    # trim old cooldown entries
    state["last_alert"] = {k: v for k, v in state["last_alert"].items()
                           if now_ts - v < 7 * 24 * 3600}
    save_state(state)
    log(f"=== done in {(datetime.now(timezone.utc)-started).seconds}s ===")


if __name__ == "__main__":
    main()
