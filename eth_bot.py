"""
ETH direction bot -> Telegram, every 15 minutes.
Uses Coinbase's free public API (no API key needed).

Setup:
  pip install requests
  export TELEGRAM_TOKEN="123456:ABC..."   # from @BotFather
  export TELEGRAM_CHAT_ID="123456789"     # your chat id
  python eth_bot.py

This is a read on current conditions, not a prediction or financial advice.
"""
import os
import time
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PRODUCT = "ETH-USD"
BASE = "https://api.exchange.coinbase.com"
INTERVAL = 15 * 60  # seconds


def get(path, **params):
    r = requests.get(f"{BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------- indicators ----------
def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def macd_hist(closes):
    fast, slow = ema(closes, 12), ema(closes, 26)
    line = [f - s for f, s in zip(fast, slow)]
    signal = ema(line, 9)
    return line[-1] - signal[-1], (line[-1] - signal[-1]) - (line[-2] - signal[-2])


# ---------- analysis ----------
def analyze():
    # Candles come back newest-first: [time, low, high, open, close, volume]
    candles = get(f"/products/{PRODUCT}/candles", granularity=900)
    candles.sort(key=lambda c: c[0])
    closes = [c[4] for c in candles]
    price = closes[-1]

    score, notes = 0, []

    # 1) Trend: EMA 9 vs 21, price vs 50
    e9, e21, e50 = ema(closes, 9)[-1], ema(closes, 21)[-1], ema(closes, 50)[-1]
    if e9 > e21:
        score += 1; notes.append("EMA9 above EMA21 (short trend up)")
    else:
        score -= 1; notes.append("EMA9 below EMA21 (short trend down)")
    if price > e50:
        score += 1; notes.append("Price above EMA50")
    else:
        score -= 1; notes.append("Price below EMA50")

    # 2) Momentum: RSI
    r = rsi(closes)
    if r > 70:
        notes.append(f"RSI {r:.0f} (overbought, pullback risk)")
    elif r < 30:
        notes.append(f"RSI {r:.0f} (oversold, bounce possible)")
    elif r > 55:
        score += 1; notes.append(f"RSI {r:.0f} (bullish momentum)")
    elif r < 45:
        score -= 1; notes.append(f"RSI {r:.0f} (bearish momentum)")
    else:
        notes.append(f"RSI {r:.0f} (neutral)")

    # 3) MACD histogram + whether it's rising
    hist, change = macd_hist(closes)
    if hist > 0 and change > 0:
        score += 1; notes.append("MACD positive and strengthening")
    elif hist < 0 and change < 0:
        score -= 1; notes.append("MACD negative and weakening")
    else:
        notes.append("MACD mixed (momentum fading)")

    # 4) Liquidity: order book imbalance within 1% of price
    book = get(f"/products/{PRODUCT}/book", level=2)
    bids = sum(float(p[1]) for p in book["bids"] if float(p[0]) >= price * 0.99)
    asks = sum(float(p[1]) for p in book["asks"] if float(p[0]) <= price * 1.01)
    imb = (bids - asks) / (bids + asks) if (bids + asks) else 0
    if imb > 0.15:
        score += 1; notes.append(f"Order book bid-heavy ({imb:+.0%})")
    elif imb < -0.15:
        score -= 1; notes.append(f"Order book ask-heavy ({imb:+.0%})")
    else:
        notes.append(f"Order book balanced ({imb:+.0%})")

    # 5) Active buyers: taker buy vs sell volume from recent trades.
    # Coinbase "side" is the MAKER side, so side == "sell" means a taker bought.
    trades = get(f"/products/{PRODUCT}/trades", limit=1000)
    buy_vol = sum(float(t["size"]) for t in trades if t["side"] == "sell")
    sell_vol = sum(float(t["size"]) for t in trades if t["side"] == "buy")
    total = buy_vol + sell_vol
    ratio = buy_vol / total if total else 0.5
    if ratio > 0.55:
        score += 1; notes.append(f"Aggressive buyers dominate ({ratio:.0%} buy volume)")
    elif ratio < 0.45:
        score -= 1; notes.append(f"Aggressive sellers dominate ({ratio:.0%} buy volume)")
    else:
        notes.append(f"Buy/sell flow even ({ratio:.0%} buy volume)")

    if score >= 4:
        verdict = "🟢 STRONG BULLISH"
    elif score >= 2:
        verdict = "🟢 Bullish"
    elif score >= -1:
        verdict = "⚪ Neutral / choppy"
    elif score >= -3:
        verdict = "🔴 Bearish"
    else:
        verdict = "🔴 STRONG BEARISH"

    msg = f"ETH ${price:,.2f}\n{verdict} (score {score:+d}/6)\n\n" + "\n".join(f"• {n}" for n in notes)
    data = {"price": price, "verdict": verdict, "score": score, "notes": notes}
    return msg, data


def save_data(data):
    """Write data.json for the dashboard (keeps the last 96 readings, ~24h)."""
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open("data.json") as f:
            history = json.load(f).get("history", [])
    except Exception:
        history = []
    history.append({"t": now, "price": data["price"], "score": data["score"]})
    out = {"updated": now, "latest": data, "history": history[-96:]}
    with open("data.json", "w") as f:
        json.dump(out, f)


def run():
    msg, data = analyze()
    send(msg)
    save_data(data)


def send(text):
    if not TOKEN or not CHAT_ID:
        print(text)
        print("\n(Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID to get this on Telegram)")
        return
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=15,
    )


if __name__ == "__main__":
    if os.environ.get("RUN_ONCE") == "1":
        # Used by GitHub Actions: run a single check and exit
        run()
        raise SystemExit
    while True:
        try:
            run()
        except Exception as e:
            print("Error:", e)
        # wait until the next 15-minute mark
        time.sleep(INTERVAL - (time.time() % INTERVAL) + 5)
